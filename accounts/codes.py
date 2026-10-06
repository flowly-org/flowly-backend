"""Invite codes: register (or redeem later) with a code and get a plan, e.g. Pro Plus forever."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import CodeRedemption, InviteCode

RANK = {"free": 0, "pro": 1, "pro_plus": 2}


class CodeError(ValueError):
    pass


def normalize(code: str) -> str:
    return "".join((code or "").split()).upper()


def find(code: str):
    """The usable code, or raise CodeError with a message for people."""
    c = InviteCode.objects.filter(code__iexact=normalize(code)).first()
    if c is None or not c.active:
        raise CodeError("This code isn't valid.")
    if c.valid_until and c.valid_until < timezone.now():
        raise CodeError("This code has expired.")
    if c.max_uses is not None and c.uses >= c.max_uses:
        raise CodeError("This code has already been used by everyone it was meant for.")
    return c


def describe(c) -> dict:
    return {"plan": c.plan, "forever": c.days is None, "days": c.days}


@transaction.atomic
def redeem(user, code: str) -> CodeRedemption:
    c = InviteCode.objects.select_for_update().filter(code__iexact=normalize(code)).first()
    if c is None:
        raise CodeError("This code isn't valid.")
    find(c.code)  # same checks, now under the lock (the last free use can't be taken twice)
    if CodeRedemption.objects.filter(code=c, user=user).exists():
        raise CodeError("You've already used this code.")
    expires = timezone.now() + timedelta(days=c.days) if c.days else None
    r = CodeRedemption.objects.create(code=c, user=user, plan=c.plan, plan_expires_at=expires)
    InviteCode.objects.filter(pk=c.pk).update(uses=c.uses + 1)
    grant = best_grant(user)
    current = RANK.get(user.plan, 0) if user.has_pro else 0
    if grant and RANK.get(grant[0], 0) >= current:  # unless they already have something better
        user.plan, user.plan_expires_at, user.plan_source = grant[0], grant[1], "code"
        user.save(update_fields=["plan", "plan_expires_at", "plan_source"])
    return r


def has_paid_subscription(user) -> bool:
    try:
        from payments.models import Subscription

        return any(s.live for s in Subscription.objects.filter(customer__user=user))
    except Exception:
        return False


def best_grant(user):
    """(plan, expires_at) of the best code the user redeemed that's still running, or None."""
    now = timezone.now()
    best = None
    for r in user.code_redemptions.all():
        if r.plan_expires_at and r.plan_expires_at < now:
            continue
        key = (RANK.get(r.plan, 0), r.plan_expires_at is None, r.plan_expires_at or now)
        if best is None or key > best[0]:
            best = (key, (r.plan, r.plan_expires_at))
    return best[1] if best else None
