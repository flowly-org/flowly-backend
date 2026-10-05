"""Outlook / Microsoft 365 calendars through Microsoft Graph: OAuth (offline access, read-only) and calendarView."""

import base64
import json
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import quote, urlencode

from django.utils import timezone

from instance import conf
from integrations.crypto import decrypt, encrypt
from integrations.http import DeliveryError, send

AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "openid email profile offline_access User.Read Calendars.Read"


def enabled() -> bool:
    return bool(conf.get("MICROSOFT_CLIENT_ID") and conf.get("MICROSOFT_CLIENT_SECRET"))


def redirect_uri() -> str:
    return f"{conf.get('API_PUBLIC_URL')}/api/calendar/microsoft/callback/"


def auth_url(state: str) -> str:
    return f"{AUTHORITY}/authorize?" + urlencode({
        "client_id": conf.get("MICROSOFT_CLIENT_ID"), "redirect_uri": redirect_uri(), "response_type": "code",
        "response_mode": "query", "scope": SCOPES, "prompt": "select_account", "state": state,
    })


def _token_request(data: dict) -> dict:
    data = {"client_id": conf.get("MICROSOFT_CLIENT_ID"), "client_secret": conf.get("MICROSOFT_CLIENT_SECRET"), "scope": SCOPES, **data}
    return send("POST", f"{AUTHORITY}/token", data=data, check_url=False).json()


def exchange_code(code: str) -> dict:
    tokens = _token_request({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri()})
    email = ""
    if tokens.get("id_token"):
        payload = tokens["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        email = claims.get("email") or claims.get("preferred_username") or ""
    tokens["email"] = email if "@" in email else ""
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
        raise DeliveryError("Not connected: sign in to Microsoft again.", retry=False)
    try:
        tokens = _token_request({"refresh_token": refresh, "grant_type": "refresh_token"})
    except DeliveryError as exc:
        if exc.status in (400, 401):
            raise DeliveryError("Microsoft access was revoked or expired: reconnect the calendar.", status=exc.status, retry=False) from exc
        raise
    store_tokens(account, tokens)
    account.save(update_fields=["access_token", "token_expires_at", "refresh_token"])
    return tokens["access_token"]


def _get(account, url: str) -> dict:
    return send("GET", url, check_url=False, timeout=20, headers={
        "Authorization": f"Bearer {access_token(account)}", "Prefer": 'outlook.timezone="UTC"'}).json()


def _paged(account, url: str, limit=10) -> list[dict]:
    out = []
    for _ in range(limit):
        data = _get(account, url)
        out += data.get("value", [])
        url = data.get("@odata.nextLink")
        if not url:
            break
    return out


def calendars(account) -> list[dict]:
    return [{"id": c["id"], "name": c.get("name") or "Calendar", "color": (c.get("hexColor") or "")[:20],
             "primary": bool(c.get("isDefaultCalendar")),
             "mine": bool(c.get("canEdit")) or bool(c.get("isDefaultCalendar"))}
            for c in _paged(account, f"{GRAPH}/me/calendars?$top=100")]


def _parse(value: str):
    # Graph: "2026-10-05T09:00:00.0000000" in the requested (UTC) zone.
    return datetime.fromisoformat(value[:19]).replace(tzinfo=dt_timezone.utc)


def events(account, calendar_id: str, lo, hi, tz) -> list[tuple[str, dict]]:
    select = "id,subject,bodyPreview,location,webLink,isAllDay,start,end,isCancelled,responseStatus"
    url = (f"{GRAPH}/me/calendars/{quote(calendar_id, safe='')}/calendarView?"
           + urlencode({"startDateTime": lo.astimezone(dt_timezone.utc).isoformat(), "endDateTime": hi.astimezone(dt_timezone.utc).isoformat(),
                        "$top": "250", "$select": select}))
    out = []
    for item in _paged(account, url):
        if item.get("isCancelled") or (item.get("responseStatus") or {}).get("response") == "declined":
            continue
        start, end = _parse(item["start"]["dateTime"]), _parse(item["end"]["dateTime"])
        if item.get("isAllDay"):
            first, last = start.date(), max(start.date(), (end - timedelta(days=1)).date())
            values = {"all_day": True, "start": start, "end": end, "start_date": first, "end_date": last}
        else:
            values = {"all_day": False, "start": start, "end": end, "start_date": start.astimezone(tz).date(),
                      "end_date": max(start, end - timedelta(microseconds=1)).astimezone(tz).date()}
        out.append((item["id"][:255], {
            **values,
            "title": (item.get("subject") or "(busy)")[:500],
            "description": (item.get("bodyPreview") or "")[:2000],
            "location": ((item.get("location") or {}).get("displayName") or "")[:500],
            "html_link": (item.get("webLink") or "")[:1000],
        }))
    return out
