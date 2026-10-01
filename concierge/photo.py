"""Photo of a hand-written (or printed) to-do list → text in the bulk-add format, read by a vision model."""

import base64
import logging
import re

from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from instance import conf
from integrations.http import DeliveryError
from workspaces.models import Membership

from .llm import LLMUnavailable, Provider, chat

log = logging.getLogger("flowly.concierge")
MAX_BYTES = 8 * 1024 * 1024
TYPES = {"image/jpeg", "image/png", "image/webp"}

PROMPT = """Transcribe the to-do list in this photo into Flowly's bulk-add format.

Rules:
- One task per line, starting with "- ". Keep the wording and the language of the list; fix only obvious misspellings.
- Items that are ticked, checked or crossed out start with "- [x] ".
- A line written indented under an item (a detail or sub-point of it, usually without its own bullet) is a note of
  that item: write it on the next line, indented by two spaces and without "- ".
- A date or weekday written next to an item: replace it with @YYYY-MM-DD at the end of the line (today is {today},
  a {weekday}; a weekday means the next one), and leave the date words out of the title.
- Only if the list clearly marks it: !{priorities} for priority, +Project for one of these projects: {projects};
  #tag for one of these tags: {tags}. Never invent projects or tags.
- Ignore headings, page decorations and anything that isn't a task.
{note}- Reply with the list only: no title, no comments, no code fences. If there is no legible to-do item, reply NONE."""


def provider_for(user, workspace):
    """The workspace's own AI provider if it has one (required when it's exclusive); else the platform key (Pro)."""
    from integrations.models import Integration

    own = (
        Integration.objects.filter(workspace=workspace, kind__in=["openrouter", "openai_compatible"], enabled=True)
        .order_by("-config__exclusive", "id")
        .first()
    )
    if own is not None:
        return Provider.from_integration(own)
    if not user.has_pro:
        raise LLMUnavailable("Reading photos uses AI: it's part of Flowly Pro, or add an AI provider to this workspace.")
    provider = Provider.platform_default()
    if provider is None:
        raise LLMUnavailable("No AI provider is configured. Ask the admin to set the OpenRouter key.")
    provider.model = conf.get("OPENROUTER_VISION_MODEL") or provider.model
    return provider


def clean(text: str) -> str:
    text = re.sub(r"^```[a-z]*\n?|\n?```$", "", (text or "").strip()).strip()
    return "" if text.upper() == "NONE" else text


def read_list(user, workspace, image: bytes, content_type: str, note: str = "") -> tuple[str, str]:
    from concierge.notify import local_today
    from tasks.models import TagGroup

    today = local_today(user)
    tags = workspace.tags.select_related("group")
    names = lambda qs: ", ".join(sorted({t.name for t in qs})[:40]) or "(none)"
    prompt = PROMPT.format(
        today=today.isoformat(), weekday=today.strftime("%A"),
        priorities="/!".join(t.name.lower() for t in tags.filter(group__kind=TagGroup.Kind.PRIORITY).order_by("position")) or "high",
        projects=names(workspace.projects.filter(archived=False)),
        tags=names(tags.exclude(group__kind__in=[TagGroup.Kind.STATUS, TagGroup.Kind.PRIORITY])),
        note=f"- The user wrote this about the photo; follow it for dates, projects or priorities: {note.strip()[:300]!r}\n"
        if note.strip() else "",
    )
    provider = provider_for(user, workspace)
    url = f"data:{content_type};base64,{base64.b64encode(image).decode()}"
    messages = [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": url}},
    ]}]
    result = chat(provider, messages, max_tokens=1500, user=user)
    return clean(result["message"].get("content") or ""), provider.model


class PhotoTasksView(APIView):
    """POST multipart `image` → {"text": "- task…"} for the bulk-add dialog to preview. Nothing is created here."""

    parser_classes = [MultiPartParser]

    def post(self, request, ws):
        membership = Membership.objects.filter(workspace_id=ws, user=request.user).select_related("workspace").first()
        if membership is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        upload = request.FILES.get("image")
        if upload is None:
            return Response({"detail": "Attach a photo."}, status=status.HTTP_400_BAD_REQUEST)
        if upload.content_type not in TYPES:
            return Response({"detail": "Use a JPEG, PNG or WebP photo."}, status=status.HTTP_400_BAD_REQUEST)
        if upload.size > MAX_BYTES:
            return Response({"detail": "The photo is too large (max 8 MB)."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            text, model = read_list(request.user, membership.workspace, upload.read(), upload.content_type)
        except LLMUnavailable as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)
        except DeliveryError as exc:
            log.warning("Photo import failed for %s: %s %s", request.user.pk, exc, exc.body[:300])
            return Response({"detail": "The AI model couldn't read the photo. Try again, or with a sharper photo."},
                            status=status.HTTP_502_BAD_GATEWAY)
        if not text:
            return Response({"detail": "No to-do items found in the photo."}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        return Response({"text": text, "model": model})
