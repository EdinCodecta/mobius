"""Typed result of one public GitHub effect, separate from its durable claim.

Only a refusal known to precede remote dispatch may release an exact claim.
Transport failures after dispatch remain unknown and must be observed, not retried.
"""
from dataclasses import dataclass
import json
import re
from typing import Generic, Literal, TypeVar

from app.contribution_errors import ContributionSubmitError

T = TypeVar("T")


class NotDispatched(Exception):
  """A proved refusal, including a provider response certifying no effect."""


@dataclass(frozen=True)
class DispatchResult(Generic[T]):
  state: Literal["not_dispatched", "unknown", "confirmed"]
  receipt: T | None = None
  reason: str = ""

  @classmethod
  def not_dispatched(cls, reason: str) -> "DispatchResult[T]":
    return cls("not_dispatched", reason=reason)

  @classmethod
  def unknown(cls, reason: str) -> "DispatchResult[T]":
    return cls("unknown", reason=reason)

  @classmethod
  def confirmed(cls, receipt: T) -> "DispatchResult[T]":
    return cls("confirmed", receipt=receipt)


_GH_HTTP_STATUS = re.compile(
  r"(?im)^(?:gh:\s*)?HTTP\s+([245][0-9]{2})(?=\s*:)"
  r"|^gh:[^\n]{0,240}\(HTTP\s+([245][0-9]{2})\)\s*$"
)
_GRAPHQL_REFUSAL_TYPES = {"FORBIDDEN", "NOT_FOUND", "UNPROCESSABLE"}


def _gh_http_status(detail: str) -> int | None:
  match = _GH_HTTP_STATUS.search(detail)
  return int(next(group for group in match.groups() if group)) if match else None


def classify_dispatch_error(exc: Exception) -> DispatchResult:
  """Classify only a *proved* provider refusal as non-effect.

  A gh transport failure, timeout, 408/429 or 5xx can have reached GitHub.
  Internal response-shape errors also cannot prove that no effect occurred.
  """
  if isinstance(exc, NotDispatched):
    return DispatchResult.not_dispatched(str(exc))
  detail = str(exc)
  if isinstance(exc, ContributionSubmitError) and not (exc.code or "").startswith("merge_response_"):
    status = _gh_http_status(detail)
    if status is None and exc.status_code != 409:
      status = exc.status_code
    if status is not None and 400 <= status < 500 and status not in (408, 429):
      return DispatchResult.not_dispatched(f"GitHub refused the request (HTTP {status}); no effect was applied.")
  return DispatchResult.unknown("GitHub did not confirm the request. Reconcile without retry.")


def graphql_mutation_result(response) -> dict:
  """Inspect a `gh api graphql` result obtained with `check=False`.

  gh exits nonzero for GraphQL errors, so its structured stdout must be read
  before a generic transport error can discard a proved no-data rejection.
  """
  try:
    result = json.loads(response.stdout)
  except json.JSONDecodeError:
    if getattr(response, "returncode", 0):
      raise ContributionSubmitError((getattr(response, "stderr", "") or "GitHub GraphQL request failed.")[:600])
    raise
  if not isinstance(result, dict):
    raise ContributionSubmitError("GitHub returned an unexpected GraphQL response.", code="merge_response_invalid_shape")
  if "data" in result and result["data"] is not None and not isinstance(result["data"], dict):
    raise ContributionSubmitError("GitHub returned malformed GraphQL data.", code="graphql_response_invalid_shape")
  if "errors" in result:
    errors = result["errors"]
    if (not isinstance(errors, list) or not errors
        or not all(isinstance(item, dict) and isinstance(item.get("message"), str)
                   and item["message"].strip() and
                   (item.get("extensions") is None or isinstance(item["extensions"], dict))
                   for item in errors)):
      raise ContributionSubmitError("GitHub returned malformed GraphQL errors.", code="graphql_response_invalid_shape")
    if "data" not in result or result["data"] is None:
      status = _gh_http_status(getattr(response, "stderr", "") or "")
      types = [item.get("type") or (item.get("extensions") or {}).get("type")
               or (item.get("extensions") or {}).get("code") for item in errors]
      if (all(isinstance(kind, str) and kind.upper() in _GRAPHQL_REFUSAL_TYPES for kind in types)
          and (status is None or 200 <= status < 300
               or (400 <= status < 500 and status not in (408, 429)))):
        raise NotDispatched("GitHub rejected the GraphQL mutation without applying it.")
      raise ContributionSubmitError("GitHub did not establish whether the GraphQL mutation applied.",
                                    code="graphql_response_uncertain")
  if getattr(response, "returncode", 0):
    raise ContributionSubmitError((getattr(response, "stderr", "") or "GitHub GraphQL request failed.")[:600])
  return result
