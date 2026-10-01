"""Google Calendar over plain HTTPS: OAuth (offline access, read-only scope) and the events API."""

import base64
import json
from datetime import timedelta
from urllib.parse import quote, urlencode

from django.utils import timezone

from instance import conf
from integrations.crypto import decrypt, encrypt
from integrations.http import DeliveryError, send

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/calendar/v3"
SCOPES = "openid email https://www.googleapis.com/auth/calendar.readonly"


def enabled() -> bool:
    return bool(conf.get("GOOGLE_CLIENT_ID") and conf.get("GOOGLE_CLIENT_SECRET"))


def redirect_uri() -> str:
    return f"{conf.get('API_PUBLIC_URL')}/api/calendar/google/callback/"


def auth_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": conf.get("GOOGLE_CLIENT_ID"), "redirect_uri": redirect_uri(), "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "select_account consent", "include_granted_scopes": "true",
        "state": state,
    })


def _token_request(data: dict) -> dict:
    data = {"client_id": conf.get("GOOGLE_CLIENT_ID"), "client_secret": conf.get("GOOGLE_CLIENT_SECRET"), **data}
    return send("POST", TOKEN_URL, data=data, check_url=False).json()


def exchange_code(code: str) -> dict:
    """→ {access_token, refresh_token, expires_in, email}"""
    tokens = _token_request({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri()})
    email = ""
    if tokens.get("id_token"):
        # Straight from Google's token endpoint over TLS: no signature check needed to read the email.
        payload = tokens["id_token"].split(".")[1]
        email = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email", "")
    tokens["email"] = email
    return tokens


def store_tokens(account, tokens: dict):
    account.access_token = encrypt(tokens["access_token"])
    account.token_expires_at = timezone.now() + timedelta(seconds=int(tokens.get("expires_in", 3600)) - 60)
    if tokens.get("refresh_token"):
        account.refresh_token = encrypt(tokens["refresh_token"])


def access_token(account) -> str:
    if account.access_token and account.token_expires_at and account.token_expires_at > timezone.now():
        return decrypt(account.access_token)
    refresh = decrypt(account.refresh_token)
    if not refresh:
        raise DeliveryError("Not connected: sign in to Google again.", retry=False)
    try:
        tokens = _token_request({"refresh_token": refresh, "grant_type": "refresh_token"})
    except DeliveryError as exc:
        if exc.status in (400, 401):
            raise DeliveryError("Google access was revoked or expired: reconnect the calendar.", status=exc.status, retry=False) from exc
        raise
    store_tokens(account, tokens)
    account.save(update_fields=["access_token", "token_expires_at", "refresh_token"])
    return tokens["access_token"]


def _get(account, path: str, params: dict | None = None) -> dict:
    url = API + path + ("?" + urlencode(params) if params else "")
    return send("GET", url, headers={"Authorization": f"Bearer {access_token(account)}"}, check_url=False, timeout=20).json()


def calendars(account) -> list[dict]:
    out, page = [], None
    while True:
        data = _get(account, "/users/me/calendarList", {"maxResults": 250, **({"pageToken": page} if page else {})})
        out += data.get("items", [])
        page = data.get("nextPageToken")
        if not page:
            return out


def events(account, calendar_id: str, time_min, time_max) -> list[dict]:
    out, page = [], None
    params = {
        "timeMin": time_min.isoformat(), "timeMax": time_max.isoformat(), "singleEvents": "true",
        "orderBy": "startTime", "maxResults": 250,
    }
    for _ in range(10):  # 2500 events in the window is plenty for a person
        data = _get(account, f"/calendars/{quote(calendar_id, safe='')}/events", {**params, **({"pageToken": page} if page else {})})
        out += data.get("items", [])
        page = data.get("nextPageToken")
        if not page:
            break
    return out


def revoke(account):
    token = decrypt(account.refresh_token) or decrypt(account.access_token)
    if token:
        try:
            send("POST", REVOKE_URL, data={"token": token}, check_url=False)
        except DeliveryError:
            pass  # already revoked: fine
