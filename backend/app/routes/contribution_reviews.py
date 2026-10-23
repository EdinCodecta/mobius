"""Owner-selected PR review runs; exact public merge is a separate DB grant."""
import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import chat_queue, chat_writer, contribution_review_runs as reviews, models, providers, transcript_rows
from app import contribution_review_presets as presets
from app import contribution_review_merge_actions as merge_actions
from app.chat_start import start_programmatic_chat_turn
from app.chat_visibility import coerce_agent_settings
from app.config import get_settings
from app.contribution_autopilot import resolve_round_choice
from app.database import get_db
from app.deps import (Principal, get_agent_run_principal, get_principal, is_owner_input_principal,
                      reject_cross_site, require_nondelegated_owner_or_app_control)
from app.github_contributions import _validate_submit_app
from app.github_contribution_git import _gh

router = APIRouter(tags=["contribution-reviews"])
APP_PREFIX = "/api/github/contributions"
SCOPE = {"correctness", "maintainability", "simplicity", "tests", "security_privacy", "technical_debt"}
REVIEW_CAPABILITIES = {"post_review": False, "review_fix_merge": False}
REVIEW_STOP_KEY = "_review_stop"


class PullIdentity(BaseModel):
  repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=256)
  number: int = Field(ge=1)
  head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class SelectedPR(PullIdentity):
  base_ref: str = Field(min_length=1, max_length=256)
  base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class ChatApproval(BaseModel):
  # The owning agent interprets consent in context; this is an auditable
  # attestation, not a keyword classifier or permission inferred from PR text.
  context: str = Field(min_length=1, max_length=2000)

  @field_validator("context")
  @classmethod
  def nonempty_context(cls, value):
    if not value.strip():
      raise ValueError("Explain the owner's explicit approval in this chat.")
    return value.strip()


class ReviewOptions(BaseModel):
  model_config = ConfigDict(extra="forbid")
  review_prompt: str | None = Field(default=None, max_length=20000)
  fix_prompt: str | None = Field(default=None, max_length=20000)
  merge_prompt: str | None = Field(default=None, max_length=20000)
  max_rounds: int | None = Field(default=None, ge=1, strict=True)
  autopilot: bool = False


class ReviewAgentChoice(BaseModel):
  model_config = ConfigDict(extra="forbid")
  provider: str = Field(max_length=32)
  model: str = Field(min_length=1, max_length=256)
  effort: str | None = Field(default=None, max_length=32)


class StartReviews(BaseModel):
  source_chat_id: str | None = Field(default=None, min_length=1, max_length=64)
  agent: ReviewAgentChoice | None = None
  preview_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
  request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
  mode: Literal["review", "review_merge"]
  options: ReviewOptions | None = None
  items: list[SelectedPR] = Field(min_length=1, max_length=20)
  chat_approval: ChatApproval | None = None


class ReviewOutcome(PullIdentity):
  state: Literal["all_clear", "needs_you"]
  summary: str = Field(min_length=1, max_length=4000)
  scope: list[str] = Field(default_factory=list, max_length=6)
  tests: str = Field(default="", max_length=4000)
  tests_passed: bool | None = None
  reviewed_base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")


def _row(db, app_id, run_id, principal):
  _authorize_context(db, app_id, principal)
  row = db.query(models.ContributionReviewRun).filter_by(
    id=run_id, app_id=app_id, owner_id=principal.owner.id,
  ).first()
  if row is None:
    raise HTTPException(404, "Review run not found.")
  return row


def _assert_execution_live(db, row, principal):
  # Recheck after awaited remote preflight; Stop, uninstall and permission
  # revocation must still win before this exact public attempt is armed.
  db.refresh(row)
  if _review_stop_requested(row):
    raise HTTPException(409, "This review was stopped. No new action was started.")
  active = db.query(models.ChatRun.id).filter_by(
    id=principal.run_id, chat_id=row.chat_id, status="running",
  ).first()
  if not active:
    raise HTTPException(409, "This review stopped or its authorization changed. No merge was started.")
  _assert_app_current(db, row.app_id, row.app_nonce)


def _assert_app_current(db, app_id, nonce):
  if app_id is None:
    return
  app = db.query(models.App).populate_existing().filter_by(id=app_id).first()
  if app is None or app.deleted_at is not None or not app.github_access or app.token_nonce != nonce:
    raise HTTPException(409, "Contribute's permission changed. No new review or merge was started.")


def _authorize_context(db, app_id, principal):
  if app_id is not None:
    return _validate_submit_app(app_id, principal, db)
  if principal.scope != "owner" or principal.app_id is not None:
    raise HTTPException(403, "Core GitHub review workflows require an owner token.")
  return None


def _fingerprint(snapshot):
  return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _options_input(body, frozen_options=None):
  if body.options is None:
    return None
  options = body.options.model_dump(exclude_none=True)
  # Keep null as an explicit uncapped selection, unlike absent prompt overrides.
  options["max_rounds"] = body.options.max_rounds
  previous = (frozen_options or {}).get("input_options") or {}
  if "max_rounds" not in body.options.model_fields_set and previous.get("max_rounds") == 5:
    # Prior requests materialized the then-default finite limit. A retry with
    # the same omitted field still names that saved grant, not today's default.
    options["max_rounds"] = 5
  return options


def _snapshot(db, body, principal):
  choice = resolve_round_choice(db)
  requested = getattr(body, "agent", None)
  if requested:
    if requested.provider not in providers.PROVIDERS or providers._model_belongs_to_other_provider(requested.model, requested.provider):
      raise HTTPException(422, "The selected provider/model does not match.")
    try:
      settings = providers.snapshot_chat_agent_settings(get_settings().data_dir,
        requested.provider, model=requested.model, effort=requested.effort,
        fallback_model=requested.model)
    except ValueError as exc:
      raise HTTPException(422, str(exc)) from exc
    choice = {"provider": requested.provider, **settings}
  if principal.chat_id and principal.run_id:
    chat = db.get(models.Chat, principal.chat_id)
    if chat:
      actual = {"provider": chat.provider, **coerce_agent_settings(chat.agent_settings_json)}
      if actual.get("model"):
        if requested and any(actual.get(k) != choice.get(k) for k in ("provider", "model", "effort")):
          raise HTTPException(409, "The owning chat is executing a different model. Preview its actual run choice.")
        choice = {k: actual[k] for k in ("provider", "model", "effort") if k in actual}
  try:
    return presets.resolve_options(db, _options_input(body), choice=choice)
  except ValueError as exc:
    raise HTTPException(422, str(exc)) from exc


def _chat_approval(db, body, principal):
  is_agent = principal.chat_id is not None or principal.run_id is not None
  if body.source_chat_id is not None and (not is_agent or body.source_chat_id != principal.chat_id):
    raise HTTPException(403, "A source chat may only name the current authenticated owning conversation.")
  if principal.delegation_id is not None:
    raise HTTPException(403, "Only the owning conversation can use chat approval, not a delegated helper.")
  if not is_agent:
    if body.chat_approval is not None:
      raise HTTPException(403, "Chat approval requires the current owning agent run.")
    return None
  if (body.chat_approval is None or principal.scope != "owner"
      or principal.app_id is not None or not principal.chat_id or not principal.run_id):
    raise HTTPException(403, "Explicit approval in the owning chat is required. Otherwise use an owner-approved selection flow.")
  live = db.query(models.ChatRun.id).join(models.Chat).filter(
    models.ChatRun.id == principal.run_id,
    models.ChatRun.chat_id == principal.chat_id,
    models.ChatRun.status == "running", models.Chat.deleted_at.is_(None),
  ).first()
  if not live:
    raise HTTPException(409, "The approving conversation is no longer running.")
  return {"source": "chat", "chat_id": principal.chat_id,
          "run_id": principal.run_id, "context": body.chat_approval.context}


@router.post(APP_PREFIX + "/{app_id}/review-runs", dependencies=[Depends(reject_cross_site),
             Depends(require_nondelegated_owner_or_app_control)])
async def start_reviews(app_id: int | None, body: StartReviews,
                        db: Session = Depends(get_db),
                        principal: Principal = Depends(get_principal)):
  approval = _chat_approval(db, body, principal)
  app_nonce = _authorize_context(db, app_id, principal)
  _assert_app_current(db, app_id, app_nonce)
  selection = sorted([{**i.model_dump(), "repo": i.repo.lower()} for i in body.items], key=reviews.key)
  if len({reviews.key(i) for i in selection}) != len(selection):
    raise HTTPException(422, "Select each pull request only once.")
  async with chat_queue.get_transition_lock(f"review-start:{principal.owner.id}:{app_id}:{body.request_id}"):
    db.rollback()
    row = db.query(models.ContributionReviewRun).filter_by(
      app_id=app_id, request_id=body.request_id, owner_id=principal.owner.id,
    ).first()
    if row is not None:
      previous = [{"repo": t["repo"].lower(), **{k: t[k] for k in ("number", "head_sha", "base_ref", "base_sha")}} for t in row.targets_json]
      if (row.mode != body.mode or previous != selection
          or (row.options_json and (
            row.options_json.get("input_options") != (_options_input(body, row.options_json) or {})
            or row.options_json.get("input_agent") != (body.agent.model_dump() if body.agent else None)
            or (body.preview_sha256 and row.options_json.get("preview_sha256") != body.preview_sha256)))):
        raise HTTPException(409, "This request already approves a different selection.")
      if approval and row.chat_id != principal.chat_id:
        raise HTTPException(409, {"message": "This selection already belongs to another review conversation. Follow it instead of starting again.",
                                  "chat_id": row.chat_id, "run_id": row.id})
    else:
      snapshot = _snapshot(db, body, principal)
      if body.preview_sha256 and body.preview_sha256 != _fingerprint(snapshot):
        raise HTTPException(409, "The resolved prompts or model changed. Preview and confirm again.")
      cwd = Path(get_settings().data_dir) / "platform"
      actor = await asyncio.to_thread(reviews.read, _gh, cwd, "user")  # unsettled: read-only (GitHub API reads)
      if not actor.get("id"):
        raise HTTPException(409, "Connect GitHub before starting this review.")
      targets = [await asyncio.to_thread(reviews.inspect_target, _gh, cwd, item, body.mode)  # unsettled: read-only (GitHub API reads)
                 for item in selection]
      # Consent in chat stays in that chat. An app selection without a source
      # conversation still receives its own durable review conversation.
      approval = _chat_approval(db, body, principal)
      _assert_app_current(db, app_id, app_nonce)
      if approval:
        chat_id = principal.chat_id
        targets = [{**target, "approval": approval} for target in targets]
      else:
        choice = snapshot["choice"]
        chat = chat_writer.create_chat(id=str(uuid.uuid4()), title="Review selected contributions",
                           provider=choice["provider"], created_by_app_id=app_id,
                           agent_settings_json={
                             key: value for key, value in choice.items()
                             if key != "provider"
                           })
        db.add(chat)
        chat_id = chat.id
      row = models.ContributionReviewRun(id=str(uuid.uuid4()), app_id=app_id,
        owner_id=principal.owner.id, request_id=body.request_id, mode=body.mode,
        targets_json=targets, outcomes_json={}, chat_id=chat_id,
        github_actor_id=str(actor["id"]),
        app_nonce=app_nonce, options_json={**snapshot, "input_options": _options_input(body) or {},
          "preview_sha256": _fingerprint(snapshot),
          "input_agent": body.agent.model_dump() if body.agent else None})
      db.add(row)
      db.commit()
      db.refresh(row)
    # Any durable first turn owns recovery; clicking twice never starts a second
    # review or revives a stopped run. An admission failure retries the empty chat.
    if not db.query(models.ChatRun).filter_by(chat_id=row.chat_id).first():
      chat = db.get(models.Chat, row.chat_id)
      frozen = row.options_json or {}
      actual = coerce_agent_settings(chat.agent_settings_json)
      if frozen.get("model") and (chat.provider != frozen["provider"] or actual.get("model") != frozen["model"] or actual.get("effort") != frozen.get("reasoning_effort")):
        raise HTTPException(409, "The saved review chat no longer matches the frozen provider/model/effort.")
      _assert_app_current(db, row.app_id, row.app_nonce)
      started = await start_programmatic_chat_turn(chat_id=chat.id, title=chat.title,
        content=reviews.brief(row), provider=chat.provider, initiated_by_app_id=app_id)
      if not started:
        raise HTTPException(503, "The review conversation could not start. Retry this same selection.")
    return {"run": _run_view(db, row), "brief": reviews.brief(row)}


@router.get(APP_PREFIX + "/{app_id}/review-runs")
def list_reviews(app_id: int | None, db: Session = Depends(get_db),
                 principal: Principal = Depends(get_principal)):
  _authorize_context(db, app_id, principal)
  rows = db.query(models.ContributionReviewRun).filter(
    models.ContributionReviewRun.owner_id == principal.owner.id,
    _read_context_filter(app_id),
  ).order_by(models.ContributionReviewRun.created_at.desc()).limit(100).all()
  return {"runs": [_run_view(db, row, context_app_id=app_id) for row in rows]}


def _last_reply_error(chat) -> str:
  """The provider's own error on the conversation's last reply, if it recorded one.

  A run that stopped on out-of-credits or a provider block should say so,
  rather than only that it stopped.
  """
  for message in reversed(transcript_rows.history(chat) if chat else []):
    if not isinstance(message, dict) or message.get("role") != "assistant":
      continue
    errors = [block["message"].strip() for block in (message.get("blocks") or [])
              if isinstance(block, dict) and block.get("type") == "error"
              and isinstance(block.get("message"), str) and block["message"].strip()]
    return errors[-1].splitlines()[0][:240] if errors else ""
  return ""


@router.post(APP_PREFIX + "/{app_id}/review-runs/{run_id}/outcomes",
             dependencies=[Depends(reject_cross_site)])
async def report_outcome(app_id: int | None, run_id: str, body: ReviewOutcome,
                         db: Session = Depends(get_db),
                         principal: Principal = Depends(get_agent_run_principal)):
  row = _row(db, app_id, run_id, principal)
  _parent(db, row, principal)
  item_key = reviews.key(body.model_dump())
  async with chat_queue.get_transition_lock(f"review-outcome:{run_id}"):
    db.refresh(row)
    original = next((t for t in row.targets_json if reviews.key(t) == item_key), None)
    target = original
    if target is None or target["head_sha"] != body.head_sha:
      raise HTTPException(409, "This head was not in the approved selection.")
    previous = row.outcomes_json.get(item_key, {})
    attempted = previous.get("merge_attempted") is True or previous.get("state") in {"merging", "merge_unknown", "queued", "merged"}
    if previous.get("state") == "merged" or (row.mode == "review" and previous.get("state") == "all_clear"):
      return {"run": _run_view(db, row)}
    if body.state == "all_clear" and (set(body.scope) != SCOPE or not body.tests.strip() or body.tests_passed is False):
      raise HTTPException(422, "Record the complete review scope and test evidence before all clear.")
    if row.mode == "review" and not attempted:
      # Private findings are facts about the selected head, not the live PR.
      # A moved or temporarily unreadable PR must not replace their evidence.
      async with chat_queue.get_transition_lock(f"review-admission:{run_id}"):
        _parent(db, row, principal)
        reviews.save_outcome(db, row, item_key, {**previous, "state": body.state,
          "summary": body.summary, "scope": body.scope, "tests": body.tests,
          "tests_passed": body.tests_passed, "reviewed_base_sha": body.reviewed_base_sha,
          "head_sha": body.head_sha, "review_chat_id": row.chat_id,
          "review_run_id": principal.run_id})
      return {"run": _run_view(db, row)}
    cwd = Path(get_settings().data_dir) / "platform"
    try:
      repo, pull = await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)  # unsettled: read-only (GitHub API reads)
    except HTTPException as exc:
      # A failed live read cannot rewrite private evidence or a public receipt.
      if not attempted:
        reviews.save_outcome(db, row, item_key, {**previous, "state": "needs_you",
          "summary": body.summary, "scope": body.scope, "tests": body.tests,
          "tests_passed": body.tests_passed, "reviewed_base_sha": body.reviewed_base_sha,
          "head_sha": body.head_sha, "review_chat_id": row.chat_id,
          "review_run_id": principal.run_id, "live_blocker": str(exc.detail)})
      return {"run": _run_view(db, row)}
    outcome = {**previous, "state": body.state, "summary": body.summary, "scope": body.scope,
               "tests": body.tests, "tests_passed": body.tests_passed,
               "reviewed_base_sha": body.reviewed_base_sha,
               "head_sha": body.head_sha,
               "review_chat_id": row.chat_id, "review_run_id": principal.run_id}
    if attempted:
      if pull.get("merged"):
        outcome = {**previous, "state": "merged", "merge_sha": pull.get("merge_commit_sha")}
      else:
        checks = await asyncio.to_thread(reviews.pull_checks, _gh, cwd, target)  # unsettled: read-only (GitHub API reads)
        entry = reviews.queue_entry(checks, target)
        if entry:
          outcome = {**previous, "state": "queued", "queue_entry_id": entry["id"]}
        else:
          outcome = {**previous, "state": "merge_unknown", "summary":
            "The earlier merge or queue request is not confirmed. Check GitHub; it was not repeated."}
      reviews.save_effect_outcome(db, row, item_key, outcome)
      return {"run": _run_view(db, row)}
    # A private review remains true of its pinned base after that base moves.
    # Only an actual merge candidate requires current-base proof.
    if body.state == "all_clear" and row.mode == "review_merge":
      await merge_actions.merge_reviewed_target(db, row, target, outcome, repo, pull, principal,
        gh=_gh, cwd=cwd, authorize=lambda: _parent(db, row, principal))
      return {"run": _run_view(db, row)}
    if not outcome.get("merge_attempted"):
      _parent(db, row, principal)
    if outcome.get("merge_attempted"):
      reviews.save_effect_outcome(db, row, item_key, outcome)
    else:
      reviews.save_outcome(db, row, item_key, outcome)
    return {"run": _run_view(db, row)}


class ReviewPreview(BaseModel):
  agent: ReviewAgentChoice | None = None
  options: ReviewOptions | None = None


@router.get(APP_PREFIX + "/{app_id}/review-presets")
def review_presets(app_id: int, db: Session = Depends(get_db),
                   principal: Principal = Depends(get_principal)):
  _authorize_context(db, app_id, principal)
  return {"presets": presets.presets(), "capabilities": REVIEW_CAPABILITIES}


@router.post(APP_PREFIX + "/{app_id}/review-preview", dependencies=[Depends(reject_cross_site)])
def review_preview(app_id: int, body: ReviewPreview, db: Session = Depends(get_db),
                   principal: Principal = Depends(get_principal)):
  _authorize_context(db, app_id, principal)
  snapshot = _snapshot(db, body, principal)
  return {"options": snapshot, "preview_sha256": _fingerprint(snapshot)}


@router.get(APP_PREFIX + "/{app_id}/review-runs/{run_id}")
def get_review(app_id: int, run_id: str, db: Session = Depends(get_db),
               principal: Principal = Depends(get_principal)):
  _authorize_context(db, app_id, principal)
  row = db.query(models.ContributionReviewRun).filter(
    models.ContributionReviewRun.id == run_id,
    models.ContributionReviewRun.owner_id == principal.owner.id,
    _read_context_filter(app_id),
  ).first()
  if row is None:
    raise HTTPException(404, "Review run not found.")
  foreign_core = app_id is not None and row.app_id is None
  return {"run": _run_view(db, row, context_app_id=app_id),
          "brief": None if foreign_core else reviews.brief(row)}


def _read_context_filter(app_id):
  """App presentation may include core work; its mutation grant stays core."""
  if app_id is None:
    return models.ContributionReviewRun.app_id.is_(None)
  return or_(models.ContributionReviewRun.app_id == app_id,
             models.ContributionReviewRun.app_id.is_(None))


def _run_view(db, row, *, context_app_id=None):
  value = reviews.view(row)
  chat = db.get(models.Chat, row.chat_id)
  value["chat_created_by_app_id"] = chat.created_by_app_id if chat else None
  stop_context = context_app_id if context_app_id is not None else row.app_id
  value["can_stop"] = bool(chat and (stop_context is None or chat.created_by_app_id == stop_context))
  value["stop_semantics"] = "revokes_future_actions; an already-admitted public action may finish"
  frozen = row.options_json or {}
  actual = coerce_agent_settings(chat.agent_settings_json) if chat else {}
  if value["state"] != "complete" and frozen.get("model") and (not chat or chat.provider != frozen["provider"] or actual.get("model") != frozen["model"] or actual.get("effort") != frozen.get("reasoning_effort")):
    value.update(state="needs_you", summary="The owning chat model choice no longer matches this frozen workflow.")
  last = db.query(models.ChatRun).filter_by(chat_id=row.chat_id).order_by(
    models.ChatRun.started_at.desc()).first()
  if value["state"] != "complete" and chat and chat.pending_question_id:
    # An open owner card is the conversation's real state, however its last
    # turn ended: it continues when the owner answers, so it is not stopped.
    value.update(state="needs_you", execution_state="awaiting_owner",
                 summary="The review conversation asked you a question. It continues when you answer.")
  elif value["state"] != "complete" and last and last.status in {"stopped", "failed", "interrupted"}:
    reason = _last_reply_error(chat)
    value.update(state="needs_you", execution_state=last.status, summary=(
      f"The review conversation stopped: {reason}" if reason
      else "The review conversation stopped. Open it to continue the remaining work."))
  elif last:
    value["execution_state"] = last.status
  if value["state"] != "complete" and _review_stop_requested(row):
    value.update(state="needs_you", execution_state="stopped",
                 summary="This review was stopped. No new evidence or public action is admitted.")
  if context_app_id is not None and row.app_id is None:
    # Read-through is navigation only. Another app's GitHub capability does
    # not grant access to core prompts, approval words or private findings.
    items = []
    for item in value["items"]:
      public = item.get("public_review") or {}
      projected = {key: item[key] for key in ("repo", "number", "head_sha", "base_ref", "base_sha", "state") if key in item}
      if public:
        projected["public_review"] = {key: public[key] for key in ("state", "url") if key in public}
      items.append(projected)
    value = {key: value[key] for key in ("id", "chat_id", "state", "execution_state", "mode", "created_at") if key in value} | {
      "app_id": None, "can_stop": False, "post_review": bool((row.options_json or {}).get("post_review")),
      "details_in_owning_chat": True, "items": items,
    }
  return value


def _review_stop_requested(row):
  return (row.outcomes_json or {}).get(REVIEW_STOP_KEY, {}).get("state") == "requested"


def _parent(db, row, principal):
  if principal.delegation_id is not None or principal.chat_id != row.chat_id:
    raise HTTPException(403, "Only this review's owning conversation may report an outcome or merge.")
  _assert_execution_live(db, row, principal)
  frozen = row.options_json or {}
  if frozen.get("model"):
    chat = db.query(models.Chat).populate_existing().filter_by(id=row.chat_id).first()
    settings = coerce_agent_settings(chat.agent_settings_json) if chat else {}
    if not chat or chat.provider != frozen["provider"] or settings.get("model") != frozen["model"] or settings.get("effort") != frozen.get("reasoning_effort"):
      raise HTTPException(409, "The owning conversation's model choice changed. This frozen workflow cannot continue under a different choice.")


@router.post("/api/github/review-runs", dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def core_start_reviews(body: StartReviews, db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  return await start_reviews(None, body, db, principal)


@router.get("/api/github/review-runs")
def core_list_reviews(db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  return list_reviews(None, db, principal)


@router.get("/api/github/review-runs/{run_id}")
def core_get_review(run_id: str, db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  return get_review(None, run_id, db, principal)


@router.get("/api/github/review-presets")
def core_review_presets(db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  _authorize_context(db, None, principal)
  return {"presets": presets.presets(), "capabilities": REVIEW_CAPABILITIES}


@router.post("/api/github/review-preview", dependencies=[Depends(reject_cross_site)])
def core_review_preview(body: ReviewPreview, db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  _authorize_context(db, None, principal)
  snapshot = _snapshot(db, body, principal)
  return {"options": snapshot, "preview_sha256": _fingerprint(snapshot)}


@router.post("/api/github/review-runs/{run_id}/outcomes", dependencies=[Depends(reject_cross_site)])
async def core_report_outcome(run_id: str, body: ReviewOutcome, db: Session = Depends(get_db), principal: Principal = Depends(get_agent_run_principal)):
  return await report_outcome(None, run_id, body, db, principal)


@router.post(APP_PREFIX + "/{app_id}/review-runs/{run_id}/stop",
             dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def stop_review(app_id: int | None, run_id: str, db: Session = Depends(get_db),
                      principal: Principal = Depends(get_principal)):
  from app.chat import stop_chat_for
  row = _row(db, app_id, run_id, principal)
  # An app may stop only the dedicated app-created review chat, never the
  # owner's chat where a conversational approval happened to be recorded.
  chat = db.get(models.Chat, row.chat_id)
  if principal.app_id is not None and (chat is None or chat.created_by_app_id != principal.app_id):
    raise HTTPException(403, "Open the owning conversation to stop this owner-chat workflow.")
  # Stopping a run stops the chat executing it. From that chat's own agent this
  # would silently end the caller's turn mid-command, never what it asked for.
  if principal.chat_id is not None and principal.chat_id == row.chat_id:
    raise HTTPException(409, "This review run belongs to your own chat, so stopping it would stop "
                             "your current turn. A superseded run is bound to its exact head and "
                             "needs no stop; start a fresh run for the new head instead.")
  # Goal holds accept only these actors; anything else reads as an unexplained interruption.
  actor = ("owner" if is_owner_input_principal(principal)
           else "agent" if principal.run_id is not None else "unknown")
  # Persist the run-level revocation before awaiting either the parent Stop or
  # child cancellation. A completed parent has no running root to mark stopped,
  # and child cancellation may wait or time out without latching cancelled_at.
  async with chat_queue.get_transition_lock(f"review-admission:{run_id}"):
    db.refresh(row)
    if not _review_stop_requested(row):
      reviews.save_outcome(db, row, REVIEW_STOP_KEY, {"state": "requested", "actor": actor})
  parent_stopped, _ = await stop_chat_for(row.chat_id, db=db, actor=actor,
                                          actor_id=principal.run_id or principal.chat_id)
  from app.delegations import ACTIVE_DELEGATION_STATUSES, derived_status
  from app.routes.delegations import cancel_active_for_parent
  await cancel_active_for_parent(db, row.chat_id)
  remaining = db.query(models.Delegation).filter(
    models.Delegation.parent_chat_id == row.chat_id,
    models.Delegation.cancelled_at.is_(None),
  ).all()
  children_stopped = not any(derived_status(db, child, load_result=False)[0]
                             in ACTIVE_DELEGATION_STATUSES for child in remaining)
  db.refresh(row)
  return {"stopped": parent_stopped and children_stopped, "run": _run_view(db, row)}


@router.post("/api/github/review-runs/{run_id}/stop",
             dependencies=[Depends(reject_cross_site), Depends(require_nondelegated_owner_or_app_control)])
async def core_stop_review(run_id: str, db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  return await stop_review(None, run_id, db, principal)


@router.post(APP_PREFIX + "/{app_id}/review-runs/{run_id}/observe",
             dependencies=[Depends(reject_cross_site)])
async def observe_review(app_id: int | None, run_id: str, db: Session = Depends(get_db),
                          principal: Principal = Depends(get_principal)):
  """Read-only GitHub reconciliation: never starts agents or public actions.

  The existing durable Wait owner may call this after resumption. It also
  settles lost receipts while a chat is stopped without reviving that chat.
  """
  if app_id is not None:
    _authorize_context(db, app_id, principal)
    foreign_core = db.query(models.ContributionReviewRun.id).filter_by(
      id=run_id, app_id=None, owner_id=principal.owner.id).first()
    if foreign_core:
      raise HTTPException(403, {"code": "details_in_owning_chat",
        "message": "Open the owning conversation to observe this core review run."})
  row = _row(db, app_id, run_id, principal)
  cwd = Path(get_settings().data_dir) / "platform"
  async with chat_queue.get_transition_lock(f"review-outcome:{run_id}"):
    db.refresh(row)
    for original in row.targets_json:
      previous = row.outcomes_json.get(reviews.key(original), {})
      attempted = previous.get("merge_attempted") is True or previous.get("state") in {"merging", "merge_unknown", "queued", "merged"}
      if not attempted:
        continue
      target = original
      try:
        _repo, pull = await asyncio.to_thread(reviews.current_pull, _gh, cwd, target)  # unsettled: read-only (GitHub API reads)
        if attempted and pull.get("merged"):
          outcome = {**previous, "state": "merged", "merge_sha": pull.get("merge_commit_sha")}
        elif attempted:
          checks = await asyncio.to_thread(reviews.pull_checks, _gh, cwd, target)  # unsettled: read-only (GitHub API reads)
          entry = reviews.queue_entry(checks, target)
          outcome = ({**previous, "state": "queued", "queue_entry_id": entry["id"]} if entry else
            {**previous, "state": "merge_unknown", "summary": "The exact earlier merge/queue attempt remains unclear. It was not repeated."})
      except HTTPException:
        # A failed read proves no new fact; historical review evidence and the
        # exact public-attempt fence both remain intact.
        continue
      if outcome != previous:
        reviews.save_effect_outcome(db, row, reviews.key(original), outcome)
    return {"run": _run_view(db, row)}


@router.post("/api/github/review-runs/{run_id}/observe", dependencies=[Depends(reject_cross_site)])
async def core_observe_review(run_id: str, db: Session = Depends(get_db), principal: Principal = Depends(get_principal)):
  return await observe_review(None, run_id, db, principal)
