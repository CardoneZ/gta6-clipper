"""Membership verdicts, webhook signatures/events, credential primitives and the API membership gate."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest

import helpers  # noqa: F401
from fastapi.testclient import TestClient

from saas import billing, security
from saas.app import Settings, create_app

NOW = 1_800_000_000
SECRET = "whsec_test_only"


def row(**kw: object) -> dict:
    base = {"plan": billing.PLAN_ID, "status": "active", "price_cents": 3000, "current_period_end": NOW + 86400}
    base.update(kw)
    return base


class EvaluateMembershipTests(unittest.TestCase):
    def test_cases(self) -> None:
        g = billing.DEFAULT_GRACE_SEC
        cases = [
            (None, False, "no_membership"),
            (row(), True, "active"),
            (row(status="trialing"), True, "active"),
            (row(current_period_end=NOW - 10), True, "renewal_pending"),
            (row(current_period_end=NOW - g - 1), False, "expired"),
            (row(status="past_due", current_period_end=NOW - 100), True, "past_due_grace"),
            (row(status="past_due", current_period_end=NOW - g - 1), False, "past_due"),
            (row(status="canceled"), False, "canceled"),
            (row(plan="free"), False, "unknown_plan"),
            (row(price_cents=2999), False, "price_mismatch"),
        ]
        for r, active, reason in cases:
            with self.subTest(reason=reason):
                v = billing.evaluate_membership(r, now=NOW)
                self.assertEqual((v.active, v.reason), (active, reason))

    def test_grace_flag(self) -> None:
        self.assertTrue(billing.evaluate_membership(row(current_period_end=NOW - 5), now=NOW).in_grace)
        self.assertFalse(billing.evaluate_membership(row(), now=NOW).in_grace)

    def test_public_carries_price(self) -> None:
        pub = billing.evaluate_membership(row(), now=NOW).public()
        self.assertEqual((pub["price_cents"], pub["currency"]), (3000, "usd"))


class WebhookSignatureTests(unittest.TestCase):
    def test_valid_tampered_expired_missing(self) -> None:
        body = b'{"a":1}'
        header = billing.sign_webhook(body, SECRET, timestamp=NOW)
        self.assertTrue(billing.verify_webhook_signature(body, header, SECRET, now=NOW + 10))
        self.assertFalse(billing.verify_webhook_signature(b'{"a":2}', header, SECRET, now=NOW))
        self.assertFalse(billing.verify_webhook_signature(body, header, "other", now=NOW))
        self.assertFalse(billing.verify_webhook_signature(body, header, SECRET, now=NOW + 301))
        self.assertFalse(billing.verify_webhook_signature(body, None, SECRET, now=NOW))
        self.assertFalse(billing.verify_webhook_signature(body, "garbage", SECRET, now=NOW))
        self.assertFalse(billing.verify_webhook_signature(body, header, "", now=NOW))

    def test_key_rotation_multiple_v1(self) -> None:
        body = b"{}"
        good = billing.sign_webhook(body, SECRET, timestamp=NOW).split(",")[1]
        self.assertTrue(billing.verify_webhook_signature(body, f"t={NOW},v1=deadbeef,{good}", SECRET, now=NOW))

    def test_event_validation(self) -> None:
        ok = {"id": "evt_1", "type": "membership.activated", "data": {"user_id": "u", "current_period_end": NOW}}
        self.assertEqual(billing.membership_from_event(ok)["status"], "active")
        bad = [
            [],
            {**ok, "type": "membership.free_money"},
            {**ok, "id": ""},
            {**ok, "data": "x"},
            {**ok, "data": {"user_id": "u", "current_period_end": True}},
            {**ok, "data": {"user_id": "u", "current_period_end": 1.5}},
            {**ok, "data": {"user_id": "", "current_period_end": NOW}},
            {**ok, "data": {"user_id": "u", "current_period_end": NOW, "price_cents": -1}},
        ]
        for ev in bad:
            with self.subTest(ev=ev), self.assertRaises(ValueError):
                billing.membership_from_event(ev)  # type: ignore[arg-type]


class SecurityPrimitiveTests(unittest.TestCase):
    def test_password_roundtrip(self) -> None:
        h = security.hash_password("correct horse battery")
        self.assertTrue(h.startswith("scrypt$"))
        self.assertTrue(security.verify_password("correct horse battery", h))
        self.assertFalse(security.verify_password("wrong", h))
        self.assertFalse(security.verify_password("x", "not$a$hash"))

    def test_session_tokens(self) -> None:
        tok = security.sign_session("user1", "s3cret", ttl_sec=60, now=NOW)
        self.assertEqual(security.read_session(tok, "s3cret", now=NOW + 30), "user1")
        self.assertIsNone(security.read_session(tok, "s3cret", now=NOW + 61))
        self.assertIsNone(security.read_session(tok, "other", now=NOW))
        self.assertIsNone(security.read_session(tok[:-2] + "xx", "s3cret", now=NOW))
        self.assertIsNone(security.read_session("nodot", "s3cret"))

    def test_api_keys(self) -> None:
        key, prefix, digest = security.new_api_key()
        self.assertTrue(key.startswith("gtc_"))
        self.assertEqual(prefix, key[:12])
        self.assertTrue(security.api_key_matches(key, digest))
        self.assertFalse(security.api_key_matches(key + "x", digest))

    def test_email_normalisation(self) -> None:
        self.assertEqual(security.normalize_email("  Pat@Example.COM "), "pat@example.com")
        self.assertIsNone(security.normalize_email("no-at-sign"))


class MembershipApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_root=Path(self.tmp.name), webhook_secret=SECRET, auth_rate=(1000, 60.0))
        self.client = TestClient(create_app(self.settings))
        self.client.__enter__()
        res = self.client.post("/api/auth/register", json={"email": "creator@example.test", "password": "longpassword1"})
        self.assertEqual(res.status_code, 200, res.text)
        self.user_id = res.json()["user_id"]
        self.auth = {"Authorization": f"Bearer {res.json()['token']}"}

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def webhook(self, event: dict, secret: str = SECRET) -> object:
        body = json.dumps(event).encode()
        return self.client.post("/api/billing/webhook", content=body,
                                headers={"X-Billing-Signature": billing.sign_webhook(body, secret), "Content-Type": "application/json"})

    def activate(self, eid: str = "evt_1", etype: str = "membership.activated", end: int | None = None) -> object:
        return self.webhook({"id": eid, "type": etype, "data": {"user_id": self.user_id, "current_period_end": end or int(time.time()) + 30 * 86400}})

    def test_gate_402_until_membership_then_open(self) -> None:
        r = self.client.post("/api/uploads", json={"filename": "a.mp4", "total_bytes": 10}, headers=self.auth)
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["detail"]["error"], "membership_required")
        self.assertFalse(self.client.get("/api/billing/verify-membership", headers=self.auth).json()["active"])
        self.assertEqual(self.activate().status_code, 200)
        v = self.client.post("/api/billing/verify-membership", headers=self.auth).json()
        self.assertTrue(v["active"])
        self.assertEqual(v["price_cents"], 3000)
        r = self.client.post("/api/uploads", json={"filename": "a.mp4", "total_bytes": 10}, headers=self.auth)
        self.assertEqual(r.status_code, 200)

    def test_cancel_event_revokes(self) -> None:
        self.activate("evt_a")
        self.activate("evt_b", "membership.canceled")
        self.assertFalse(self.client.get("/api/billing/verify-membership", headers=self.auth).json()["active"])

    def test_webhook_idempotent(self) -> None:
        self.assertFalse(self.activate("evt_dup").json()["duplicate"])
        self.assertTrue(self.activate("evt_dup").json()["duplicate"])

    def test_webhook_rejects_bad_signature_and_unknown_user(self) -> None:
        ev = {"id": "e", "type": "membership.activated", "data": {"user_id": self.user_id, "current_period_end": NOW}}
        self.assertEqual(self.webhook(ev, secret="forged").status_code, 400)
        ev2 = {"id": "e2", "type": "membership.activated", "data": {"user_id": "nobody", "current_period_end": NOW}}
        self.assertEqual(self.webhook(ev2).status_code, 404)
        self.assertEqual(self.webhook({"id": "e3", "type": "nope", "data": {}}).status_code, 422)

    def test_unauthenticated_401(self) -> None:
        self.assertEqual(self.client.get("/api/billing/verify-membership").status_code, 401)
        self.assertEqual(self.client.get("/api/me", headers={"Authorization": "Bearer forged.token"}).status_code, 401)

    def test_api_key_auth_and_revoke(self) -> None:
        self.activate()
        made = self.client.post("/api/keys", json={"label": "obs-plugin"}, headers=self.auth).json()
        kh = {"X-API-Key": made["key"]}
        self.assertTrue(self.client.get("/api/billing/verify-membership", headers=kh).json()["active"])
        listed = self.client.get("/api/keys", headers=self.auth).json()["keys"]
        self.assertNotIn("key_hash", listed[0])
        self.assertEqual(self.client.delete(f"/api/keys/{made['id']}", headers=self.auth).status_code, 200)
        self.assertEqual(self.client.get("/api/me", headers=kh).status_code, 401)

    def test_login_flow_and_errors(self) -> None:
        ok = self.client.post("/api/auth/login", json={"email": "CREATOR@example.test", "password": "longpassword1"})
        self.assertEqual(ok.status_code, 200)
        bad = self.client.post("/api/auth/login", json={"email": "creator@example.test", "password": "nope"})
        self.assertEqual(bad.status_code, 401)
        ghost = self.client.post("/api/auth/login", json={"email": "ghost@example.test", "password": "nope"})
        self.assertEqual(ghost.json(), bad.json())
        dup = self.client.post("/api/auth/register", json={"email": "creator@example.test", "password": "longpassword1"})
        self.assertEqual(dup.status_code, 409)
        short = self.client.post("/api/auth/register", json={"email": "x@example.test", "password": "short"})
        self.assertEqual(short.status_code, 422)

    def test_dev_activate_disabled_by_default(self) -> None:
        self.assertEqual(self.client.post("/api/billing/dev-activate", json={}, headers=self.auth).status_code, 404)
        self.assertFalse(self.client.get("/api/health").json()["dev_billing"])


class RateLimitTests(unittest.TestCase):
    def test_auth_rate_limited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, TestClient(create_app(Settings(data_root=Path(tmp), auth_rate=(3, 60.0)))) as c:
            codes = [c.post("/api/auth/login", json={"email": "a@example.test", "password": "x"}).status_code for _ in range(5)]
        self.assertEqual(codes, [401, 401, 401, 429, 429])


if __name__ == "__main__":
    unittest.main()
