"""Meeting notes, transcripts and emails → action items in the bulk-add format (who does what, by when)."""

import logging
import re

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from instance import conf
from integrations.http import DeliveryError
from workspaces.models import Membership

from .llm import LLMUnavailable, Provider, chat

log = logging.getLogger("flowly.concierge")
MAX_CHARS = 60_000

PROMPT = """Extract the action items from this {kind} and write them in Flowly's bulk-add format.

Rules:
- One task per line, starting with "- ", as a short imperative ("Send the offer to Rossi"). Write in the language of
  the {kind}. Only real commitments or requests someone has to act on; skip discussion, decisions without a follow-up,
  and things already done.
- Who does it: add =Name, written exactly as listed, for one of these people when the {kind} says so (match them by
  first name too): {members}. If it's
  someone else, put their name in the title instead. {self_rule}
- When: add @YYYY-MM-DD for a deadline or day that is stated or clearly implied (today is {today}, a {weekday}; a weekday
  means the next one). No date if none is given.
- Optionally !{priorities} when urgency is explicit, +Project for one of these projects when it obviously fits:
  {projects}. Never invent people, projects or dates.
- If useful, add one indented line under a task (two spaces, no "- ") with the context needed to act (who asked, which
  client, the detail that matters). Sub-steps can be indented "[ ] step" lines.
- Reply with the list only: no title, no comments, no code fences. If there is nothing to do, reply NONE."""


def text_provider(user, workspace, what="This") -> Provider:
    """The workspace's own AI provider (Enterprise) if any, else the platform key for Pro users."""
    from integrations.models import Integration

    own = (Integration.objects.filter(workspace=workspace, kind__in=["openrouter", "openai_compatible"], enabled=True)
           .order_by("-config__exclusive", "id").first()) if workspace.is_enterprise else None
    if own is not None:
        return Provider.from_integration(own)
    if not user.has_pro:
        raise LLMUnavailable(f"{what} uses AI: it's part of Flowly Pro, or add an AI provider to this workspace.")
    provider = Provider.platform_default()
    if provider is None:
        raise LLMUnavailable("No AI provider is configured. Ask the admin to set the OpenRouter key.")
    return provider


def clean(text: str) -> str:
    text = re.sub(r"^```[a-z]*\n?|\n?```$", "", (text or "").strip()).strip()
    return "" if text.upper().startswith("NONE") else text


def extract(user, workspace, text: str, *, kind="meeting notes", today=None, provider=None, about="") -> str:
    """Bulk-add text of the action items ("" when there are none). Raises LLMUnavailable / DeliveryError."""
    from accounts.models import User
    from tasks.models import Tag

    from .notify import local_today

    today = today or local_today(user)
    people = [u for u in User.objects.filter(memberships__workspace=workspace, is_bot=False)]
    members = ", ".join((u.full_name or u.email.split("@")[0]).replace(" ", "-") for u in people) or "nobody"
    priorities = "|!".join(t.name.lower() for t in Tag.objects.filter(workspace=workspace, group__kind="priority")) or "high"
    projects = ", ".join(p.name.replace(" ", "-") for p in workspace.projects.filter(archived=False)) or "none"
    me = user.full_name or user.email.split("@")[0]
    self_rule = (f"Things {me} (the person importing this) must do get =" + me.replace(" ", "-") + "."
                 if kind != "email" else f"Requests addressed to the recipient are for {me}: =" + me.replace(" ", "-") + ".")
    prompt = PROMPT.format(kind=kind, members=members, self_rule=self_rule, today=today.isoformat(),
                           weekday=today.strftime("%A"), priorities=priorities, projects=projects)
    body = (about + "\n\n" if about else "") + text[:MAX_CHARS]
    provider = provider or text_provider(user, workspace, "Reading notes")
    result = chat(provider, [{"role": "system", "content": prompt}, {"role": "user", "content": body}], max_tokens=4000, user=user)
    return clean(result["message"].get("content") or "")


class NotesTasksView(APIView):
    """POST {text} → {"text": "- task…"} for the bulk-add dialog to preview. Nothing is created here."""

    def post(self, request, ws):
        membership = Membership.objects.filter(workspace_id=ws, user=request.user).select_related("workspace").first()
        if membership is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        text = str(request.data.get("text") or "").strip()
        if len(text) < 20:
            return Response({"detail": "Paste the meeting notes or transcript first."}, status=status.HTTP_400_BAD_REQUEST)
        if len(text) > MAX_CHARS:
            return Response({"detail": f"That's long: at most {MAX_CHARS // 1000}k characters."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            out = extract(request.user, membership.workspace, text)
        except LLMUnavailable as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)
        except DeliveryError as exc:
            log.warning("Notes import failed for %s: %s %s", request.user.pk, exc, exc.body[:300])
            return Response({"detail": "The AI model couldn't read the notes. Try again."}, status=status.HTTP_502_BAD_GATEWAY)
        if not out:
            return Response({"detail": "No action items found in these notes."}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        return Response({"text": out})
