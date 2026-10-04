"""Flowly's apps for Flo to hand out: links to the latest builds, and the files themselves on Telegram."""

import logging
import time

import requests

log = logging.getLogger("flowly.concierge")
REPO = "flowly-org/flowly-releases"
DOWNLOAD = f"https://github.com/{REPO}/releases/latest/download/"
APPS_PAGE = "https://app.flowly.net/apps"
#: Telegram bots can upload documents up to 50 MB.
TELEGRAM_MAX = 49 * 1024 * 1024

PLATFORMS = {
    "android": {"label": "Android", "files": ["Flowly-Android.apk"],
                "note": "Open the file on the phone and allow installing from this app when asked."},
    "mac": {"label": "macOS", "files": ["Flowly-macOS-universal.dmg"],
            "note": "First time: right-click Flowly in Applications and choose Open (the app isn't signed yet)."},
    "windows": {"label": "Windows", "files": ["Flowly-Windows-x64-setup.exe", "Flowly-Windows-x64.msi"],
                "note": "If SmartScreen warns you: More info → Run anyway."},
    "linux": {"label": "Linux", "files": ["Flowly-Linux-x86_64.AppImage", "Flowly-Linux-amd64.deb", "Flowly-Linux-x86_64.rpm"],
              "note": "AppImage: make it executable (chmod +x) and run it."},
    "ios": {"label": "iPhone & iPad", "files": [],
            "note": "No App Store version yet: open https://app.flowly.net in Safari, tap Share, then Add to Home Screen."},
}
ALIASES = {"macos": "mac", "osx": "mac", "apple": "mac", "win": "windows", "pc": "windows", "iphone": "ios", "ipad": "ios",
           "phone": "android", "desktop": "desktop"}

_cache: dict = {"at": 0.0, "data": None}


def latest() -> dict:
    """{"version": "0.3.0", "sizes": {file: bytes}} from GitHub (cached for an hour; empty on failure)."""
    if _cache["data"] is None or time.monotonic() - _cache["at"] > 3600:
        try:
            res = requests.get(f"https://api.github.com/repos/{REPO}/releases/latest", timeout=10,
                               headers={"Accept": "application/vnd.github+json"})
            res.raise_for_status()
            body = res.json()
            data = {"version": str(body.get("tag_name", "")).lstrip("v"), "sizes": {a["name"]: a["size"] for a in body.get("assets", [])}}
        except (requests.RequestException, ValueError) as exc:
            log.warning("GitHub release lookup failed: %s", exc)
            data = {"version": "", "sizes": {}}
        _cache.update(data=data, at=time.monotonic())
    return _cache["data"]


def resolve(platform: str) -> list[str]:
    key = ALIASES.get((platform or "all").lower().strip(), (platform or "all").lower().strip())
    if key == "desktop":
        return ["mac", "windows", "linux"]
    if key in PLATFORMS:
        return [key]
    return list(PLATFORMS)


def mb(size: int | None) -> str:
    return f" ({size / 1_000_000:.0f} MB)" if size else ""


def describe(platform: str) -> str:
    info = latest()
    lines = [f"Flowly apps{' ' + info['version'] if info['version'] else ''} (all of them: {APPS_PAGE}):"]
    for key in resolve(platform):
        p = PLATFORMS[key]
        if p["files"]:
            links = ", ".join(f"{DOWNLOAD}{f}{mb(info['sizes'].get(f))}" for f in p["files"])
            lines.append(f"- {p['label']}: {links}. {p['note']}")
        else:
            lines.append(f"- {p['label']}: {p['note']}")
    return "\n".join(lines)


def sendable(platform: str) -> list[str]:
    """Files small enough to send on Telegram (the main file per platform)."""
    info = latest()
    out = []
    for key in resolve(platform):
        for f in PLATFORMS[key]["files"][:1]:
            size = info["sizes"].get(f)
            if size is None or size <= TELEGRAM_MAX:
                out.append(f)
    return out


def send_file(chat_id: int, filename: str):
    """Send a release file as a Telegram document, reusing Telegram's file_id once uploaded."""
    from . import telegram
    from .models import BotState

    version = latest()["version"] or "latest"
    key = f"tgfile:{filename}:{version}"[:50]
    cached = BotState.objects.filter(key=key).first()
    caption = f"Flowly {version} · {filename}"
    if cached:
        try:
            if telegram.call("sendDocument", chat_id=chat_id, document=cached.value, caption=caption):
                return
        except Exception as exc:  # the id may have expired: upload again
            log.info("Cached Telegram file %s failed: %s", filename, exc)
    res = requests.get(DOWNLOAD + filename, timeout=60, stream=True)
    res.raise_for_status()
    data = res.raw.read(TELEGRAM_MAX + 1, decode_content=True)
    if len(data) > TELEGRAM_MAX:
        raise ValueError(f"{filename} is too big for Telegram.")
    message = telegram.upload_document(chat_id, filename, data, caption)
    file_id = ((message or {}).get("document") or {}).get("file_id")
    if file_id:
        BotState.objects.update_or_create(key=key, defaults={"value": file_id[:200]})
