import re

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
        return {"allowed_senders": senders, "schedule": clean_schedule(config)}

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
        if external_id and linked_task(integration, external_id):
            log.status = Delivery.Status.IGNORED
            log.response_body = "Duplicate message; already imported."
            log.save()
            return log
        body = text.strip()[:20000]
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
