"""Domains, the connection for an email, and finishing a sign-in (create or link the account, join the workspace)."""

import re

import requests
from django.db import transaction
from django.utils import timezone

from .models import SsoConnection, SsoDomain

DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
#: Mailbox providers nobody can claim for their organization.
PUBLIC = set("""
gmail.com googlemail.com outlook.com hotmail.com hotmail.it live.com live.it msn.com yahoo.com yahoo.it ymail.com
icloud.com me.com mac.com aol.com proton.me protonmail.com pm.me gmx.com gmx.de gmx.net web.de libero.it virgilio.it
tiscali.it alice.it tim.it fastwebnet.it email.it inwind.it mail.com yandex.com yandex.ru zoho.com fastmail.com
tutanota.com tuta.io hey.com qq.com 163.com naver.com orange.fr free.fr laposte.net sfr.fr t-online.de
""".split())
DOH = "https://cloudflare-dns.com/dns-query"


class SsoError(Exception):
    pass


def clean_domain(value: str) -> str:
    domain = (value or "").strip().lower().removeprefix("@").rstrip(".")
    domain = domain.split("@")[-1]
    if not DOMAIN.match(domain):
        raise SsoError("That doesn't look like a domain (e.g. acme.com).")
    if domain in PUBLIC:
        raise SsoError(f"{domain} is a public email provider: it can't belong to one organization.")
    return domain


def txt_records(name: str) -> list[str]:
    """TXT records via DNS over HTTPS (no resolver library needed, same answer from every server)."""
    try:
        res = requests.get(DOH, params={"name": name, "type": "TXT"}, headers={"Accept": "application/dns-json"}, timeout=8)
        res.raise_for_status()
        answers = res.json().get("Answer") or []
    except (requests.RequestException, ValueError) as exc:
        raise SsoError("Couldn't look up DNS right now. Try again in a minute.") from exc
    return ["".join(re.findall(r'"([^"]*)"', a.get("data", ""))) or a.get("data", "").strip('"') for a in answers if a.get("type") == 16]


def verify_domain(domain: SsoDomain) -> SsoDomain:
    taken = SsoDomain.objects.filter(domain=domain.domain, verified_at__isnull=False).exclude(pk=domain.pk).first()
    if taken:
        raise SsoError(f"{domain.domain} is already verified by another workspace.")
    if domain.record_value not in txt_records(domain.record_name):
        raise SsoError(f"No TXT record {domain.record_value} found at {domain.record_name} yet. DNS changes can take a few minutes.")
    domain.verified_at = timezone.now()
    domain.save(update_fields=["verified_at"])
    return domain


def connection_for_email(email: str, *, enabled_only=True) -> SsoConnection | None:
    """The workspace connection that vouches for this email's domain (Enterprise workspaces only)."""
    domain = (email or "").strip().lower().rsplit("@", 1)[-1]
    if "@" not in (email or "") or not domain:
        return None
    match = SsoDomain.objects.filter(domain=domain, verified_at__isnull=False).select_related("connection__workspace").first()
    if match is None:
        return None
    conn = match.connection
    if not conn.workspace.is_enterprise or (enabled_only and not conn.enabled):
        return None
    return conn


def requires_sso(email: str, user=None) -> SsoConnection | None:
    """The connection that forbids password sign-in for this email (instance admins keep a break-glass password)."""
    conn = connection_for_email(email)
    if conn is None or not conn.required:
        return None
    if user is not None and user.is_staff:
        return None
    return conn


@transaction.atomic
def sign_in(connection: SsoConnection, email: str, claims: dict, *, test=False):
    """Find or create the person, add them to the workspace, and record a successful test. Returns the user."""
    from accounts.models import User
    from workspaces.models import Membership
    from workspaces.services import onboard_user

    domain = email.rsplit("@", 1)[-1]
    if not connection.domains.filter(domain=domain, verified_at__isnull=False).exists():
        raise SsoError(f"{email} isn't on a domain verified for {connection.workspace.name}.")
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        name = claims.get("name") or " ".join(filter(None, [claims.get("given_name"), claims.get("family_name")]))
        user = User.objects.create_user(email=email, password=None, full_name=(name or email.split("@")[0])[:150])
        onboard_user(user)
    elif not user.is_active:
        raise SsoError("This account is deactivated. Ask your administrator.")
    joined = False
    if connection.auto_join:
        _, joined = Membership.objects.get_or_create(workspace=connection.workspace, user=user,
                                                     defaults={"role": Membership.Role.MEMBER})
    if test:
        is_admin = Membership.objects.filter(workspace=connection.workspace, user=user,
                                             role__in=[Membership.Role.OWNER, Membership.Role.ADMIN]).exists()
        if is_admin:
            connection.tested_at, connection.tested_by, connection.last_error = timezone.now(), user, ""
            connection.save(update_fields=["tested_at", "tested_by", "last_error", "updated_at"])
    return user
