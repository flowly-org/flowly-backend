"""A small Stripe REST client (no SDK: plain HTTPS, form-encoded params, webhook signatures)."""

import hashlib
import hmac
import time

import requests

API = "https://api.stripe.com/v1/"


class StripeError(Exception):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status, self.code = status, code


def flatten(params: dict, prefix: str = "") -> list[tuple[str, str]]:
    """{"line_items": [{"price": "p", "quantity": 1}]} → [("line_items[0][price]", "p"), ("line_items[0][quantity]", "1")]"""
    out = []
    for key, value in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if value is None:
            continue
        if isinstance(value, dict):
            out += flatten(value, name)
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    out += flatten(item, f"{name}[{i}]")
                else:
                    out.append((f"{name}[{i}]", _scalar(item)))
        else:
            out.append((name, _scalar(value)))
    return out


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def call(key: str, method: str, path: str, params: dict | None = None, idempotency_key: str | None = None) -> dict:
    if not key:
        raise StripeError("Stripe isn't configured.")
    headers = {"Authorization": f"Bearer {key}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    data = flatten(params or {})
    try:
        if method == "GET":
            res = requests.get(API + path, params=data, headers=headers, timeout=20)
        else:
            res = requests.request(method, API + path, data=data, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise StripeError(f"Stripe is unreachable: {exc}") from exc
    body = res.json() if res.content else {}
    if res.status_code >= 400:
        err = body.get("error") or {}
        raise StripeError(err.get("message") or f"Stripe error {res.status_code}", status=res.status_code, code=err.get("code"))
    return body


def verify_signature(payload: bytes, header: str, secret: str, tolerance: int = 300) -> bool:
    """Stripe-Signature: t=<ts>,v1=<hex hmac of "<ts>.<payload>">[,v1=…]"""
    if not secret or not header:
        return False
    parts = [p.split("=", 1) for p in header.split(",") if "=" in p]
    ts = next((v for k, v in parts if k == "t"), "")
    sigs = [v for k, v in parts if k == "v1"]
    if not ts.isdigit() or abs(time.time() - int(ts)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def period_end(sub: dict) -> int | None:
    """current_period_end moved from the subscription to its items in newer API versions: read either."""
    if sub.get("current_period_end"):
        return sub["current_period_end"]
    items = (sub.get("items") or {}).get("data") or []
    return max((i.get("current_period_end") or 0 for i in items), default=None) or None
