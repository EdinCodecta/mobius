"""First-open consent: derived rows, the dormant-until-accepted gate, and the
human-only acceptance call."""

import json

import pytest
from sqlalchemy import create_engine, text

from app import app_consent, auth, models
from app.schema_migrations import _add_app_accepted_consent

STORE_URL = "https://example.test/store-app/mobius.json"


def _contract(**data):
  base = {
    "chat_logs": {"requested": "none", "effective": "none", "redaction": "none"},
    "filesystem_api": False, "shared_memory": "none", "cross_app_access": "none",
    "share_with_apps": "none", "manage_apps": False, "manage_skills": False,
    "github_access": False, "github_connect": False, "connections_manage": False,
    "connect_manage": False, "identity_manage": False, "railway_manage": False,
  }
  base.update(data)
  return {"schema": 6, "agent": {"skills": [], "tools": [], "embeds_agent": False,
          "system_prompt": None}, "data": base, "runtime": {}, "public": {"network": []}}


def _app(db, slug, *, contract, store=True, accepted=None):
  row = models.App(
    name=slug.title(), description="", jsx_source="", compiled_path="",
    source_dir=f"/tmp/{slug}", slug=slug,
    manifest_url=STORE_URL + slug if store else None,
    capability_contract=contract, accepted_consent=accepted,
  )
  db.add(row)
  db.commit()
  db.refresh(row)
  return row


def _owner_headers(client, owner_token):
  return {"Authorization": f"Bearer {owner_token}"}


def test_rows_are_derived_and_critical_is_platform_defined():
  contract = _contract(
    chat_logs={"requested": "summary", "effective": "summary", "redaction": "structural"},
    manage_apps=True,
  )
  contract["agent"]["embeds_agent"] = True
  rows = {r["id"]: r for r in app_consent.rows_from_contract(contract)}
  assert rows["chat_log_access"]["critical"] is False
  assert rows["manage_apps"]["critical"] is True
  assert rows["agent_start"]["critical"] is True
  # Most serious first.
  ordered = [r["critical"] for r in app_consent.rows_from_contract(contract)]
  assert ordered == sorted(ordered, reverse=True)


def test_runtime_capability_wording_comes_from_the_contract():
  contract = _contract()
  contract["runtime"] = {"media.microphone.capture": {"title": "Record audio", "description": "Use the microphone."}}
  (row,) = app_consent.rows_from_contract(contract)
  assert (row["title"], row["critical"]) == ("Record audio", False)


def test_unaccepted_store_app_is_dormant_until_accepted(client, owner_token, db):
  headers = _owner_headers(client, owner_token)
  app = _app(db, "newapp", contract=_contract(github_access=True))
  denied = client.post("/api/auth/app-token", json={"app_id": app.id}, headers=headers)
  assert denied.status_code == 409
  assert denied.json()["detail"]["code"] == "consent_required"
  job = client.post("/api/auth/app-job-token", json={"app_id": app.id}, headers=headers)
  assert job.status_code == 409

  view = client.get(f"/api/apps/{app.id}/consent", headers=headers).json()
  assert view["status"] == "required" and view["is_update"] is False
  assert [r["id"] for r in view["rows"]] == ["github_access"]
  assert view["baseline"]

  wrong = client.post(f"/api/apps/{app.id}/consent", json={"digest": "nope"}, headers=headers)
  assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "consent_changed"

  ok = client.post(f"/api/apps/{app.id}/consent", json={"digest": view["digest"]}, headers=headers)
  assert ok.status_code == 200 and ok.json()["status"] == "accepted"
  allowed = client.post("/api/auth/app-token", json={"app_id": app.id}, headers=headers)
  assert allowed.status_code == 200
  assert client.get(f"/api/apps/{app.id}/consent", headers=headers).json()["status"] == "accepted"


def test_agent_tokens_cannot_accept(client, owner_token, db, chat):
  app = _app(db, "agentcheck", contract=_contract(github_access=True))
  headers = _owner_headers(client, owner_token)
  digest = client.get(f"/api/apps/{app.id}/consent", headers=headers).json()["digest"]
  agent = auth.create_agent_token(chat.id, "test", 0)
  response = client.post(
    f"/api/apps/{app.id}/consent", json={"digest": digest},
    headers={"Authorization": f"Bearer {agent}"},
  )
  assert response.status_code == 403
  assert "Agent tokens cannot supply owner input" in response.text
  db.refresh(app)
  assert app.accepted_consent is None


def test_local_apps_are_not_gated(client, owner_token, db):
  app = _app(db, "localapp", contract=_contract(manage_apps=True), store=False)
  headers = _owner_headers(client, owner_token)
  assert client.get(f"/api/apps/{app.id}/consent", headers=headers).json()["status"] == "not_applicable"
  assert client.post("/api/auth/app-token", json={"app_id": app.id}, headers=headers).status_code == 200


def test_update_asks_only_when_it_needs_more(db):
  old = _contract(github_access=True)
  app = _app(db, "updater", contract=old, accepted=app_consent.platform_acceptance(old))
  assert app_consent.status(app) == "accepted"
  # Same or narrower request: nothing to ask.
  app.capability_contract = _contract()
  assert app_consent.status(app) == "accepted"
  # A wider request asks again, and shows only the new row.
  app.capability_contract = _contract(github_access=True, manage_apps=True)
  assert app_consent.status(app) == "required"
  assert [r["id"] for r in app_consent.needed_rows(app)] == ["manage_apps"]
  # A higher level of the same row is new.
  chat = _contract(chat_logs={"requested": "summary", "effective": "summary", "redaction": "structural"})
  app.capability_contract = chat
  app.accepted_consent = app_consent.platform_acceptance(chat)
  app.capability_contract = _contract(chat_logs={"requested": "summary_with_deleted", "effective": "summary_with_deleted", "redaction": "structural"})
  assert [r["id"] for r in app_consent.needed_rows(app)] == ["chat_log_access"]


def test_notes_name_other_holders_and_pairing(db):
  _app(db, "reader", contract=_contract(chat_logs={"requested": "summary", "effective": "summary", "redaction": "structural"}, cross_app_access="read"),
       accepted=app_consent.platform_acceptance(_contract(chat_logs={"requested": "summary", "effective": "summary", "redaction": "structural"}, cross_app_access="read")))
  _app(db, "sharer", contract=_contract(share_with_apps="read"),
       accepted=app_consent.platform_acceptance(_contract(share_with_apps="read")))
  newcomer = _app(db, "newcomer", contract=_contract(
    chat_logs={"requested": "summary_with_deleted", "effective": "summary_with_deleted", "redaction": "structural"},
    cross_app_access="read", share_with_apps="read"))
  notes = {r["id"]: r["note"] for r in app_consent.build_request(db, newcomer)["rows"]}
  assert "Also held by Reader" in notes["chat_log_access"] and "hold only" in notes["chat_log_access"]
  assert "Sharer" in notes["cross_app_access"]
  assert "Reader could read it" in notes["share_with_apps"]


def test_migration_grandfathers_existing_apps_and_is_idempotent():
  engine = create_engine("sqlite://")
  contract = _contract(github_access=True)
  with engine.begin() as conn:
    conn.execute(text("CREATE TABLE apps (id INTEGER PRIMARY KEY, capability_contract JSON, project_templates_json JSON)"))
    conn.execute(text("INSERT INTO apps (id, capability_contract, project_templates_json) VALUES (1, :c, NULL), (2, NULL, NULL)"), {"c": json.dumps(contract)})
  _add_app_accepted_consent(engine)
  _add_app_accepted_consent(engine)
  with engine.connect() as conn:
    rows = dict(conn.execute(text("SELECT id, accepted_consent FROM apps")).fetchall())
  record = json.loads(rows[1])
  assert record["via"] == "migration" and record["contract"] == contract
  assert rows[2] is None


def test_every_contract_permission_has_a_row():
  """A permission added to the contract without a popup row must fail here."""
  from app.app_capabilities import contract_from_manifest
  manifest = {
    "permissions": {
      "chat_log_access": "summary_with_deleted", "filesystem_access": True,
      "cross_app_access": "write", "share_with_apps": "write",
      "shared_memory": "write", "manage_apps": True, "manage_skills": True,
      "github_access": True, "github_connect": True, "connections_manage": True,
      "connect_manage": True, "identity_manage": True, "railway_manage": True,
      "job_secret_read": ["TOKEN"],
    },
  }
  contract = contract_from_manifest(manifest)
  unhandled = set(contract["data"]) - app_consent.HANDLED_DATA_KEYS
  assert not unhandled, f"contract.data keys without a consent row: {sorted(unhandled)}"
  ids = {r["id"] for r in app_consent.rows_from_contract(contract)}
  assert {"chat_log_access", "shared_memory", "cross_app_access", "share_with_apps",
          "filesystem_access", "manage_apps", "manage_skills", "github_access",
          "github_connect", "connections_manage", "connect_manage",
          "identity_manage", "railway_manage", "job_secret_read"} <= ids


def test_acceptance_stores_rows_and_survives_a_rewording(db, monkeypatch):
  contract = _contract(github_access=True)
  app = _app(db, "snapshot", contract=contract)
  digest = app_consent.request_digest(app_consent.current_rows(app))
  assert app_consent.accept(app, digest)
  record = app.accepted_consent
  assert record["rows"] == [{"id": "github_access", "rank": 1}] and record["version"] == 1
  assert "contract" not in record
  # Rewording a row must not turn the old answer into a new question.
  monkeypatch.setattr(app_consent, "_FLAGS", tuple(
    (k, i, "Changed title", "Changed text") for k, i, *_ in app_consent._FLAGS))
  assert app_consent.status(app) == "accepted"


def test_migrated_records_with_a_contract_copy_still_work(db):
  contract = _contract(github_access=True)
  app = _app(db, "legacy", contract=contract, accepted={
    "contract": contract, "project_builders": False, "digest": None,
    "at": "2026-10-20T00:00:00Z", "via": "migration",
  })
  assert app_consent.status(app) == "accepted"
  app.capability_contract = _contract(github_access=True, manage_apps=True)
  assert [r["id"] for r in app_consent.needed_rows(app)] == ["manage_apps"]


def test_revoke_makes_the_app_dormant_again_and_is_human_only(client, owner_token, db, chat):
  headers = _owner_headers(client, owner_token)
  contract = _contract(github_access=True)
  app = _app(db, "revokable", contract=contract, accepted=app_consent.platform_acceptance(contract))
  agent = {"Authorization": "Bearer " + auth.create_agent_token(chat.id, "test", 0)}
  assert client.delete(f"/api/apps/{app.id}/consent", headers=agent).status_code == 403
  assert client.get(f"/api/apps/{app.id}/consent", headers=headers).json()["status"] == "accepted"
  assert client.delete(f"/api/apps/{app.id}/consent", headers=headers).status_code == 200
  assert client.get(f"/api/apps/{app.id}/consent", headers=headers).json()["status"] == "required"
  assert client.post("/api/auth/app-token", json={"app_id": app.id}, headers=headers).status_code == 409


def test_install_preview_carries_the_same_rows(client, owner_token):
  headers = _owner_headers(client, owner_token)
  manifest = {
    "id": "previewed", "name": "Previewed", "version": "1.0.0",
    "description": "x", "entry": "index.jsx",
    "permissions": {"manage_apps": True},
  }
  response = client.post(
    "/api/apps/preview",
    json={"manifest": manifest, "raw_base": "https://example.test/previewed/"},
    headers=headers,
  )
  assert response.status_code == 200, response.text
  rows = response.json()["access_rows"]
  assert [(r["id"], r["critical"]) for r in rows] == [("manage_apps", True)]


def test_platform_apps_are_accepted_at_first_boot(db):
  contract = _contract(manage_apps=True)
  acceptance = app_consent.platform_acceptance(contract)
  app = _app(db, "platformapp", contract=contract, accepted=acceptance)
  assert acceptance["via"] == "platform"
  assert app_consent.status(app) == "accepted"
