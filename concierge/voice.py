"""Voice messages to the concierge (Telegram): transcribe them, then answer as if the user had typed the words.

So a dictated list becomes tasks (Flo calls add_tasks), and "move the proposal to Friday" just works.
"""

import base64
import logging

from instance import conf
from integrations.http import DeliveryError

from . import brain, telegram
from .i18n import t
from .llm import LLMUnavailable, Provider, chat

log = logging.getLogger("flowly.concierge")
MAX_SECONDS = 5 * 60
DEFAULT_MODEL = "~google/gemini-flash-latest"
FORMATS = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a",
           "audio/m4a": "m4a", "audio/aac": "aac", "audio/wav": "wav", "audio/x-wav": "wav", "audio/flac": "flac"}
PROMPT = ("Transcribe this voice message word for word, in the language it is spoken in. Output only the transcript, "
          "without quotes or comments. If there is no speech, output NONE.")


def audio_provider(user) -> Provider:
    if not user.has_pro:
        raise LLMUnavailable("Voice messages use AI: they're part of Flowly Pro.")
    provider = Provider.platform_default()
    if provider is None:
        raise LLMUnavailable("No AI provider is configured. Ask the admin to set the OpenRouter key.")
    provider.model = conf.get("OPENROUTER_AUDIO_MODEL") or DEFAULT_MODEL
    return provider


def transcribe(user, audio: bytes, fmt: str = "ogg", provider=None) -> str:
    provider = provider or audio_provider(user)
    content = [{"type": "text", "text": PROMPT},
               {"type": "input_audio", "input_audio": {"data": base64.b64encode(audio).decode(), "format": fmt}}]
    result = chat(provider, [{"role": "user", "content": content}], max_tokens=3000, user=user)
    text = (result["message"].get("content") or "").strip().strip('"“”')
    return "" if text.upper() == "NONE" else text


def format_of(mime: str) -> str:
    return FORMATS.get((mime or "audio/ogg").split(";")[0].strip().lower(), "ogg")


def handle(profile, chat_id: int, file_id: str, mime: str, duration: int, caption: str = ""):
    from .bot import deliver

    user = profile.user
    code = brain.lang(user)
    reason = brain.availability(user)
    if reason:
        return telegram.send_message(chat_id, reason)
    if duration and duration > MAX_SECONDS:
        return telegram.send_message(chat_id, t("voice_too_long", code, minutes=MAX_SECONDS // 60))
    telegram.call("sendChatAction", chat_id=chat_id, action="typing")
    try:
        audio = telegram.download(file_id, max_bytes=20 * 1024 * 1024)
        text = transcribe(user, audio, format_of(mime))
    except LLMUnavailable as exc:
        return telegram.send_message(chat_id, str(exc))
    except (DeliveryError, ValueError) as exc:
        log.warning("Voice message failed for %s: %s", user.pk, exc)
        return telegram.send_message(chat_id, t("error", code))
    if not text:
        return telegram.send_message(chat_id, t("voice_none", code))
    request = f"{caption}\n\n{text}".strip() if caption else text
    outbox: list[dict] = []
    answer = brain.reply(user, f"(voice message) {request}", channel="telegram", outbox=outbox)
    heard = text if len(text) <= 300 else text[:297] + "…"
    telegram.send_message(chat_id, f"🎙 “{heard}”\n\n{answer}")
    deliver(chat_id, outbox)
