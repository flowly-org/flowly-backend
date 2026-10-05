import re
import secrets

from django.utils import timezone

from instance import conf

from ..inbound import find_member, linked_task, upsert_task
from ..models import Delivery
from .base import Connector, clean_schedule

ADDRESS_RE = re.compile(r"[\w.+-]+@[\w.-]+")


def parse_address(value: str) -> str:
    match = ADDRESS_RE.search(value or "")
    return match.group(0).lower() if match else ""


ALIAS_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,38}[a-z0-9])?$")
TOKEN_LIKE = re.compile(r"^[0-9a-f]{16,}$")
RESERVED = {
    "abuse", "admin", "administrator", "hostmaster", "info", "mailer-daemon", "no-reply", "noreply", "postmaster",
    "root", "security", "support", "webmaster", "www", "flowly", "help", "billing",
}
MAX_ALIASES = 10


def clean_alias(value: str) -> str:
    """Normalise and check an alias; raises ValueError with a readable message."""
    name = (value or "").strip().lower()
    if "@" in name:
        name = name.split("@", 1)[0]
    if not ALIAS_RE.match(name):
        raise ValueError(f"“{value}”: use 1–40 letters, digits, dots, dashes or underscores.")
    if name in RESERVED or TOKEN_LIKE.match(name) or "+" in name:
        raise ValueError(f"“{name}” is reserved.")
    return name


def resolve(recipient: str):
    """The enabled Email-in integration an address belongs to: alias (with or without +tag), or token."""
    from ..models import EmailAlias, Integration

    local = parse_address(recipient).split("@", 1)[0]
    if not local:
        return None
    qs = Integration.objects.filter(kind="email", enabled=True).select_related("workspace", "project")
    for name in dict.fromkeys([local, local.split("+", 1)[0]]):
        alias = EmailAlias.objects.filter(name=name, integration__in=qs).select_related("integration").first()
        if alias:
            return qs.get(pk=alias.integration_id)
    return qs.filter(token=local.rsplit("+", 1)[-1]).first()


def token_from_recipient(recipient: str) -> str:
    """'<token>@domain' or 'anything+<token>@domain' → token."""
    local = parse_address(recipient).split("@", 1)[0]
    return local.rsplit("+", 1)[-1]


class Email(Connector):
    """Email sent to the integration's address becomes a task."""

    kind = "email"

    def clean_config(self, config, integration):
        senders = config.get("allowed_senders", "members")
        if senders not in ("members", "anyone"):
            senders = "members"
        ai = config.get("ai", "off")
        if ai not in ("off", "auto", "always"):
            ai = "off"
        return {"allowed_senders": senders, "schedule": clean_schedule(config), "ai": ai}

    def info(self, integration):
        domain = conf.get("EMAIL_INBOUND_DOMAIN")
        return {
            "address": f"{integration.token}@{domain}" if domain else None,
            "aliases": [f"{a.name}@{domain}" for a in integration.aliases.all()] if domain else [],
            "endpoint": f"{conf.get("API_PUBLIC_URL")}/api/integrations/email/inbound/",
            "configured": bool(domain),
        }

    def receive(self, integration, *, sender, subject, text, message_id="") -> Delivery:
        log = Delivery(
            integration=integration,
            direction=Delivery.Direction.IN,
            event="email.received",
            payload={"from": sender, "subject": subject[:200]},
            finished_at=timezone.now(),
        )
        member = find_member(integration, email=sender)
        if integration.config.get("allowed_senders", "members") == "members" and member is None:
            log.status = Delivery.Status.IGNORED
            log.response_body = f"{sender} is not a member of this workspace."
            log.save()
            return log
        external_id = f"email:{message_id}" if message_id else None
        if external_id and (linked_task(integration, external_id) or linked_task(integration, f"{external_id}:0")):
            log.status = Delivery.Status.IGNORED
            log.response_body = "Duplicate message; already imported."
            log.save()
            return log
        body = text.strip()[:20000]
        created = self.interpret(integration, member, sender=sender, subject=subject, body=body, external_id=external_id)
        if created:
            log.task = created[0]
            log.status = Delivery.Status.SUCCESS
            log.response_body = f"Created {len(created)} task(s) from the email's action items (AI)."
            log.save()
            return log
        fields = {
            "title": (subject.strip() or "(no subject)")[:255],
            "description": f"{body}\n\n— emailed by {sender}" if body else f"— emailed by {sender}",
            "assignee": member,
        }
        task, _ = upsert_task(integration, fields, external_id=external_id)
        log.task = task
        log.status = Delivery.Status.SUCCESS
        log.response_body = "Created task."
        log.save()
        return log

    # --- AI: one task per action item -----------------------------------------------------------

    LONG = 500

    def wants_ai(self, integration, subject: str, body: str) -> bool:
        mode = integration.config.get("ai", "off")
        if mode == "always":
            return bool(body)
        forwarded = subject.lower().startswith(("fwd:", "fw:", "i:", "tr:", "wg:", "rv:", "enc:")) or "forwarded message" in body.lower()
        return mode == "auto" and (len(body) > self.LONG or forwarded)

    def interpret(self, integration, member, *, sender, subject, body, external_id):
        """Tasks for the action items of the email, or [] to fall back to a single task."""
        import logging

        from concierge.llm import LLMUnavailable
        from concierge.notes import extract, text_provider
        from integrations.http import DeliveryError
        from tasks.bulk import parse

        if not self.wants_ai(integration, subject, body):
            return []
        log = logging.getLogger("flowly.integrations")
        workspace = integration.workspace
        payer = None
        for candidate in (member, integration.created_by):
            if candidate is None:
                continue
            try:
                provider = text_provider(candidate, workspace, "Reading emails")
                payer = candidate
                break
            except LLMUnavailable:
                continue
        if payer is None:
            return []
        today = timezone.localdate()
        try:
            text = extract(payer, workspace, body, kind="email", today=today, provider=provider,
                           about=f"From: {sender}\nSubject: {subject}")
        except (LLMUnavailable, DeliveryError) as exc:
            log.warning("Email AI failed for integration %s: %s", integration.pk, exc)
            return []
        drafts = parse(text, workspace, today) if text else []
        tasks = []
        for i, d in enumerate(drafts[:20]):
            when = d["date"] if d["date"] is not ... else (today if integration.config.get("schedule") == "today" else None)
            notes = "\n".join(filter(None, [d["notes"], f"— from an email by {sender}: “{subject.strip() or '(no subject)'}”"]))
            fields = {"title": d["title"], "description": notes, "assignee": d.get("assignee") or member,
                      "planned_date": when, "estimate_minutes": d.get("estimate"),
                      "checklist": [{"id": secrets.token_hex(4), **item} for item in d.get("checklist") or []]}
            if d["priority"]:
                fields["priority"] = d["priority"]
            if d["project"]:
                fields["project"] = d["project"]
            task, _ = upsert_task(integration, fields, external_id=f"{external_id}:{i}" if external_id else None, tags=d["tags"] or None)
            tasks.append(task)
        return tasks
