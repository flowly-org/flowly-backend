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
            "endpoint": f"{conf.get("PUBLIC_URL")}/api/integrations/email/inbound/",
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
