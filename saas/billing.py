"""
Creator membership ($30/month) verification and provider-neutral billing webhooks.

The payment processor (Stripe, Paddle, Lemon Squeezy...) stays outside this codebase: it calls
`POST /api/billing/webhook` with a signed event, and this module turns that into a membership
row. The signature scheme is the widely used `t=<unix>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">`
header with a 5 minute replay window; adapt `verify_webhook_signature` to your provider's exact
header if it differs. Card data never touches this service.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import time
from typing import Any

PLAN_ID = "creator_monthly"
PLAN_PRICE_CENTS = 3000  # $30.00 / month
PLAN_CURRENCY = "usd"
ACTIVE_STATUSES = ("active", "trialing")
GRACE_STATUSES = ("past_due",)  # card retry window: keep access for `grace_sec`
DEFAULT_GRACE_SEC = 3 * 24 * 3600
WEBHOOK_TOLERANCE_SEC = 300
EVENT_STATUS = {
    "membership.activated": "active",
    "membership.renewed": "active",
    "membership.trialing": "trialing",
    "membership.past_due": "past_due",
    "membership.canceled": "canceled",
}


@dataclass(frozen=True)
class MembershipVerdict:
    active: bool
    reason: str
    plan: str | None = None
    status: str | None = None
    current_period_end: int | None = None
    in_grace: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "active": self.active, "reason": self.reason, "plan": self.plan, "status": self.status,
            "current_period_end": self.current_period_end, "in_grace": self.in_grace,
            "price_cents": PLAN_PRICE_CENTS, "currency": PLAN_CURRENCY,
        }


def evaluate_membership(row: dict[str, Any] | None, now: float | None = None, grace_sec: int = DEFAULT_GRACE_SEC) -> MembershipVerdict:
    """Pure membership decision used by the `/verify-membership` endpoint and the API gate."""
    t = int(now if now is not None else time.time())
    if not row:
        return MembershipVerdict(False, "no_membership")
    plan, status, end = row.get("plan"), str(row.get("status") or ""), int(row.get("current_period_end") or 0)
    base = {"plan": plan, "status": status, "current_period_end": end}
    if plan != PLAN_ID:
        return MembershipVerdict(False, "unknown_plan", **base)
    if int(row.get("price_cents") or 0) < PLAN_PRICE_CENTS:
        return MembershipVerdict(False, "price_mismatch", **base)
    if status in ACTIVE_STATUSES:
        if end >= t:
            return MembershipVerdict(True, "active", **base)
        if end + grace_sec >= t:
            return MembershipVerdict(True, "renewal_pending", in_grace=True, **base)
        return MembershipVerdict(False, "expired", **base)
    if status in GRACE_STATUSES:
        if end + grace_sec >= t:
            return MembershipVerdict(True, "past_due_grace", in_grace=True, **base)
        return MembershipVerdict(False, "past_due", **base)
    return MembershipVerdict(False, status or "inactive", **base)


def sign_webhook(body: bytes, secret: str, timestamp: int | None = None) -> str:
    """Header value a provider (or a test) would send for `body`."""
    ts = int(timestamp if timestamp is not None else time.time())
    mac = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("ascii") + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify_webhook_signature(body: bytes, header: str | None, secret: str, now: float | None = None,
                             tolerance_sec: int = WEBHOOK_TOLERANCE_SEC) -> bool:
    if not header or not secret:
        return False
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        ts = int(parts.get("t", [""])[0])
    except ValueError:
        return False
    if abs(int(now if now is not None else time.time()) - ts) > tolerance_sec:
        return False
    want = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("ascii") + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(want, sig) for sig in parts.get("v1", []))


def membership_from_event(event: dict[str, Any]) -> dict[str, Any]:
    """
    Validate a billing event (`{id, type, data: {user_id, current_period_end, price_cents?, provider_ref?}}`)
    and return the membership row fields. Raises `ValueError` on anything malformed.
    """
    if not isinstance(event, dict):
        raise ValueError("event must be an object")
    etype, eid, data = event.get("type"), event.get("id"), event.get("data")
    if etype not in EVENT_STATUS:
        raise ValueError(f"unsupported event type {etype!r}")
    if not isinstance(eid, str) or not 1 <= len(eid) <= 128:
        raise ValueError("event id must be a 1-128 char string")
    if not isinstance(data, dict):
        raise ValueError("event data must be an object")
    user_id, end = data.get("user_id"), data.get("current_period_end")
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("data.user_id required")
    if not isinstance(end, int) or isinstance(end, bool) or end <= 0:
        raise ValueError("data.current_period_end must be a positive unix time")
    price = data.get("price_cents", PLAN_PRICE_CENTS)
    if not isinstance(price, int) or isinstance(price, bool) or price < 0:
        raise ValueError("data.price_cents must be a non-negative integer")
    ref = data.get("provider_ref")
    if ref is not None and (not isinstance(ref, str) or len(ref) > 128):
        raise ValueError("data.provider_ref must be a string up to 128 chars")
    return {
        "event_id": eid, "user_id": user_id, "plan": PLAN_ID, "status": EVENT_STATUS[etype],
        "price_cents": price, "currency": PLAN_CURRENCY, "period_end": end, "provider_ref": ref,
    }
