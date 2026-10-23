"""Typed result of one public GitHub effect, separate from its durable claim.

Only a refusal known to precede remote dispatch may release an exact claim.
Transport failures after dispatch remain unknown and must be observed, not retried.
"""
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

T = TypeVar("T")


class NotDispatched(Exception):
  """A proved local/preflight refusal before any remote mutation began."""


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
