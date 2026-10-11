"""Runtime settings that admins can change from the Admin page.

Resolution order: database override (InstanceSetting) → environment / Django settings → spec default.
Values are cached per process for a few seconds, so the API and the worker pick up changes quickly.
"""

import threading
import time
from dataclasses import dataclass, field

from django.conf import settings

from integrations.crypto import decrypt, encrypt, mask

CACHE_SECONDS = 10


@dataclass(frozen=True)
class Spec:
    key: str
    group: str
    label: str
    help: str = ""
    type: str = "str"  # str | int | bool | url | choice
    secret: bool = False
    choices: tuple = field(default_factory=tuple)
    #: Exposed to the browser (e.g. Google client id); never true for secrets.
    public: bool = False


SPECS = [
    Spec("PUBLIC_URL", "General", "App URL", "Where people open Flowly, e.g. https://app.flowly.net. Used in links and GitHub callbacks.", "url"),
    Spec("API_PUBLIC_URL", "General", "API URL", "Public URL of this API, e.g. https://api.flowly.net. Used for webhooks.", "url"),
    Spec("PLANS_ENABLED", "General", "Sell plans", "On: features follow the Free, Pro, Pro Plus and Enterprise plans, with Stripe billing. Off: everyone gets everything (self-hosted).", "bool", public=True),
    Spec("OPENROUTER_API_KEY", "AI", "OpenRouter API key", "Platform key used by Pro users' concierge.", secret=True),
    Spec("OPENROUTER_MODEL", "AI", "Default model", "An OpenRouter model id that supports tool calling."),
    Spec("OPENROUTER_AUDIO_MODEL", "AI", "Voice model", "Model that transcribes voice messages (must accept audio). Empty = ~google/gemini-flash-latest."),
    Spec("OPENROUTER_VISION_MODEL", "AI", "Photo model", "Model that reads photos of to-do lists (must accept images). Empty = the default model."),
    Spec("OPENROUTER_BASE_URL", "AI", "OpenRouter base URL", "Change only for an OpenRouter-compatible proxy.", "url"),
    Spec("CONCIERGE_DAILY_TOKENS", "AI", "Daily tokens per user", "Cap on the platform key, per user per day.", "int"),
    Spec("NOMINATIM_URL", "Travel", "Nominatim server", "Finds addresses for real driving times (opt-in per person). The public server allows light use; your own Nominatim can live on a private address.", "url"),
    Spec("OSRM_URL", "Travel", "OSRM server", "Computes driving routes. The public demo server allows light use; for more, point this at your own osrm-routed (a private address is fine), e.g. http://osrm.internal:5000.", "url"),
    Spec("OSRM_PROFILE", "Travel", "OSRM profile", "The profile in the route URL: driving on the public server; often car on your own (whatever your osrm-routed or proxy expects)."),
    Spec("TELEGRAM_BOT_TOKEN", "Telegram", "Bot token", "From @BotFather.", secret=True),
    Spec("TELEGRAM_MODE", "Telegram", "Receive messages by", "Polling works anywhere; webhook is instant but needs the API URL over HTTPS.", "choice", choices=("polling", "webhook")),
    Spec("TELEGRAM_WEBHOOK_SECRET", "Telegram", "Webhook secret", "Generated automatically when you enable the webhook.", secret=True),
    Spec("STRIPE_SECRET_KEY", "Stripe", "Secret key", "Flowly's own Stripe account (sk_live_… or sk_test_…). Then use Admin → Stripe → Set up.", secret=True),
    Spec("STRIPE_WEBHOOK_SECRET", "Stripe", "Webhook signing secret", "Filled in by Set up (whsec_…).", secret=True),
    Spec("STRIPE_PRICE_PRO_MONTH", "Stripe", "Pro monthly price id", "Filled in by Set up (price_…)."),
    Spec("STRIPE_PRICE_PRO_YEAR", "Stripe", "Pro yearly price id", "Filled in by Set up (price_…)."),
    Spec("STRIPE_PRICE_PLUS_MONTH", "Stripe", "Pro Plus monthly price id", "Filled in by Set up (price_…)."),
    Spec("STRIPE_PRICE_PLUS_YEAR", "Stripe", "Pro Plus yearly price id", "Filled in by Set up (price_…)."),
    Spec("STRIPE_PRODUCT_ENTERPRISE", "Stripe", "Enterprise product id", "Filled in by Set up (prod_…)."),
    Spec("STRIPE_PORTAL_CONFIG", "Stripe", "Customer portal configuration", "Filled in by Set up (bpc_…)."),
    Spec("STRIPE_AUTOMATIC_TAX", "Stripe", "Stripe Tax", "Compute VAT automatically (activate Stripe Tax in the Stripe dashboard first).", "bool"),
    Spec("DELEGATION_MODEL", "AI", "Delegation model", "Model that works on delegated tasks (Pro Plus); needs tool calling. Empty = the default model."),
    Spec("DELEGATION_TRIAGE_MODEL", "AI", "Delegation triage model", "Cheap model that decides whether a task can be delegated. Empty = the default model."),
    Spec("DELEGATION_SEARCH_MODEL", "AI", "Web search model", "OpenRouter model id used for web searches, e.g. one ending in :online. Empty = the delegation model + :online."),
    Spec("SLACK_CLIENT_ID", "Slack app", "Client ID", "From your Slack app (Basic Information). Create the app from the manifest shown under Admin → Slack."),
    Spec("SLACK_CLIENT_SECRET", "Slack app", "Client secret", secret=True),
    Spec("SLACK_SIGNING_SECRET", "Slack app", "Signing secret", "Verifies requests coming from Slack.", secret=True),
    Spec("EMAIL_INBOUND_DOMAIN", "Email in", "Inbound domain", "Mail to <token>@this-domain becomes tasks, e.g. in.flowly.net."),
    Spec("EMAIL_INBOUND_KEY", "Email in", "Inbound key", "Shared secret your mail provider sends as ?key= or X-Flowly-Key.", secret=True),
    Spec("S3_BUCKET", "Storage", "Bucket", "Enables file uploads."),
    Spec("S3_ENDPOINT_URL", "Storage", "Endpoint URL", "Empty for AWS; e.g. https://<account>.r2.cloudflarestorage.com.", "url"),
    Spec("S3_REGION", "Storage", "Region", "e.g. auto (R2), eu-west-1."),
    Spec("S3_ACCESS_KEY_ID", "Storage", "Access key id"),
    Spec("S3_SECRET_ACCESS_KEY", "Storage", "Secret access key", secret=True),
    Spec("S3_ADDRESSING_STYLE", "Storage", "Addressing style", "path for MinIO; virtual or empty otherwise.", "choice", choices=("", "path", "virtual")),
    Spec("ATTACHMENT_MAX_MB", "Storage", "Max upload size (MB)", type="int"),
    Spec("GOOGLE_CLIENT_ID", "Documents", "Google OAuth client id", "Web client; authorised JS origin = the app URL.", public=True),
    Spec("GOOGLE_API_KEY", "Documents", "Google API key (browser)", "Restricted to the Picker API and your app URL.", public=True),
    Spec("GOOGLE_APP_ID", "Documents", "Google app id", "Your Google Cloud project number.", public=True),
    Spec("GOOGLE_CLIENT_SECRET", "Documents", "Google OAuth client secret", "Enables Google Calendar. Add <API URL>/api/calendar/google/callback/ as an authorised redirect URI and enable the Calendar API.", secret=True),
    Spec("MICROSOFT_CLIENT_SECRET", "Documents", "Microsoft Entra client secret", "Enables Outlook / Microsoft 365 calendars with the same app: add a Web platform with redirect URI <API URL>/api/calendar/microsoft/callback/, delegated permissions Calendars.Read, User.Read, offline_access, and a client secret.", secret=True),
    Spec("MICROSOFT_CLIENT_ID", "Documents", "Microsoft Entra client id", "SPA app; redirect URI = the app URL + /.", public=True),
    Spec("INTEGRATIONS_ALLOW_PRIVATE_URLS", "Integrations", "Allow private network URLs", "Lets webhooks and AI endpoints reach private IPs (only for self-hosted, trusted setups).", "bool"),
]
SPEC_BY_KEY = {s.key: s for s in SPECS}

_cache: dict = {"rows": None, "at": 0.0}
_lock = threading.Lock()


def _rows() -> dict[str, str]:
    with _lock:
        if _cache["rows"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return _cache["rows"]
    from .models import InstanceSetting

    try:
        rows = dict(InstanceSetting.objects.values_list("key", "value"))
    except Exception:  # table missing (migrations not run yet)
        rows = {}
    with _lock:
        _cache.update(rows=rows, at=time.monotonic())
    return rows


def clear_cache():
    with _lock:
        _cache.update(rows=None, at=0.0)


def _coerce(spec: Spec, raw):
    if spec.type == "int":
        return int(raw or 0)
    if spec.type == "bool":
        return raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes", "on")
    if spec.type == "url":
        return (raw or "").rstrip("/")
    return raw if raw is not None else ""


def source(key: str) -> str:
    if key in _rows():
        return "admin"
    return "environment" if getattr(settings, key, None) not in (None, "", False) else "unset"


def get(key: str):
    spec = SPEC_BY_KEY[key]
    rows = _rows()
    if key in rows:
        raw = decrypt(rows[key]) if spec.secret else rows[key]
    else:
        raw = getattr(settings, key, "")
    value = _coerce(spec, raw)
    if key == "API_PUBLIC_URL" and not value:
        return get("PUBLIC_URL")
    return value


def set_value(key: str, value, user=None):
    """Store an override; None removes it (back to the environment value)."""
    from .models import InstanceSetting

    spec = SPEC_BY_KEY[key]
    if value is None:
        InstanceSetting.objects.filter(key=key).delete()
    else:
        text = str(value).strip() if not isinstance(value, bool) else ("1" if value else "0")
        if spec.type == "int":
            text = str(int(text))
        if spec.choices and text not in spec.choices:
            raise ValueError(f"{spec.label}: choose one of {', '.join(c or '(empty)' for c in spec.choices)}.")
        if spec.type == "url" and text and not text.startswith(("http://", "https://")):
            raise ValueError(f"{spec.label}: enter a URL starting with http(s)://.")
        InstanceSetting.objects.update_or_create(
            key=key, defaults={"value": encrypt(text) if spec.secret and text else text, "updated_by": user}
        )
    clear_cache()


def describe(spec: Spec) -> dict:
    value = get(spec.key)
    shown = (mask(value) if value else "") if spec.secret else value
    return {
        "key": spec.key, "group": spec.group, "label": spec.label, "help": spec.help, "type": spec.type,
        "secret": spec.secret, "choices": list(spec.choices), "value": shown, "is_set": bool(value),
        "source": source(spec.key),
    }


def plans_enabled() -> bool:
    """Whether this instance sells plans; when it doesn't, every account and workspace has every feature."""
    return bool(get("PLANS_ENABLED"))
