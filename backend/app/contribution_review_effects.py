"""Typed result of one public GitHub effect, separate from its durable claim.

Only a refusal known to precede remote dispatch may release an exact claim.
Transport failures after dispatch remain unknown and must be observed, not retried.
"""
from dataclasses import dataclass
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


_HTTP_STATUS = re.compile(r"\bHTTP\s+([45][0-9]{2})\b", re.IGNORECASE)


def classify_dispatch_error(exc: Exception) -> DispatchResult:
  """Classify only a *proved* provider refusal as non-effect.

  A gh transport failure, timeout, 408/429 or 5xx can have reached GitHub.
  Internal response-shape errors also cannot prove that no effect occurred.
  """
  if isinstance(exc, NotDispatched):
    return DispatchResult.not_dispatched(str(exc))
  detail = str(exc)
  if isinstance(exc, ContributionSubmitError) and not (exc.code or "").startswith("merge_response_"):
    match = _HTTP_STATUS.search(detail)
    status = int(match.group(1)) if match else (exc.status_code if exc.status_code != 409 else None)
    if status is not None and 400 <= status < 500 and status not in (408, 429):
      return DispatchResult.not_dispatched(f"GitHub refused the request (HTTP {status}); no effect was applied.")
  return DispatchResult.unknown("GitHub did not confirm the request. Reconcile without retry.")
