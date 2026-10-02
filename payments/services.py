"""Stripe flows. Flowly's account: Pro (self-serve Checkout + portal) and Enterprise (sales-led, invoiced).
The user's own account: payment links on the invoices they send to their clients."""

import logging
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from instance import conf
from integrations.crypto import decrypt, encrypt

from . import stripe_api
from .models import Customer, InvoicePayment, Subscription
from .stripe_api import StripeError

log = logging.getLogger("flowly.payments")
GRACE = timedelta(days=3)
PRO_PRICES = {"month": 600, "year": 6000}  # cents, before VAT
PLUS_PRICES = {"month": 1000, "year": 10000}
#: plan → (conf key prefix, prices, product name, description)
PLANS = {
    "pro": ("STRIPE_PRICE_PRO", PRO_PRICES, "Flowly Pro", "Personal concierge, AI features and invoices."),
    "pro_plus": ("STRIPE_PRICE_PLUS", PLUS_PRICES, "Flowly Pro Plus", "Everything in Pro, plus tasks delegated to an AI agent."),
}
RANK = {"pro": 1, "pro_plus": 2}
EVENTS = ["checkout.session.completed", "customer.subscription.created", "customer.subscription.updated",
          "customer.subscription.deleted", "invoice.payment_failed", "invoice.paid"]


def key() -> str:
    return conf.get("STRIPE_SECRET_KEY")


def enabled() -> bool:
    return bool(key() and conf.get("STRIPE_PRICE_PRO_MONTH") and conf.get("STRIPE_WEBHOOK_SECRET"))


def api(method, path, params=None, **kw):
    return stripe_api.call(key(), method, path, params, **kw)


# --- set-up -----------------------------------------------------------------------


def setup() -> dict:
    """Create (once) the products, prices, customer portal and webhook endpoint on Flowly's Stripe account."""
    done = {}
    for plan, (prefix, prices, name, description) in PLANS.items():
        if conf.get(f"{prefix}_MONTH") and conf.get(f"{prefix}_YEAR"):
            continue
        product = api("POST", "products", {"name": name, "description": description, "metadata": {"flowly": plan}},
                      idempotency_key=f"flowly-product-{plan}")
        for interval, cents in prices.items():
            price = api("POST", "prices", {
                "product": product["id"], "currency": "eur", "unit_amount": cents, "recurring": {"interval": interval},
                "tax_behavior": "exclusive", "lookup_key": f"flowly_{plan}_{interval}", "transfer_lookup_key": True,
            })
            conf.set_value(f"{prefix}_{interval.upper()}", price["id"])
        done[plan] = product["id"]
    if not conf.get("STRIPE_PRODUCT_ENTERPRISE"):
        product = api("POST", "products", {"name": "Flowly Enterprise", "description": "Team bots and the workspace's own AI provider.",
                                           "metadata": {"flowly": "enterprise"}}, idempotency_key="flowly-product-enterprise")
        conf.set_value("STRIPE_PRODUCT_ENTERPRISE", product["id"])
        done["enterprise"] = product["id"]
    if not conf.get("STRIPE_PORTAL_CONFIG"):
        portal = api("POST", "billing_portal/configurations", {
            "business_profile": {"headline": "Flowly: manage your subscription"},
            "default_return_url": f"{conf.get('PUBLIC_URL')}/settings#plan",
            "features": {
                "customer_update": {"enabled": True, "allowed_updates": ["email", "address", "name", "tax_id"]},
                "invoice_history": {"enabled": True},
                "payment_method_update": {"enabled": True},
                "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
            },
        })
        conf.set_value("STRIPE_PORTAL_CONFIG", portal["id"])
        done["portal"] = portal["id"]
    url = f"{conf.get('API_PUBLIC_URL')}/api/billing/stripe/webhook/"
    existing = api("GET", "webhook_endpoints", {"limit": 100}).get("data", [])
    if not conf.get("STRIPE_WEBHOOK_SECRET") or not any(e["url"] == url for e in existing):
        for endpoint in existing:
            if endpoint["url"] == url:
                api("DELETE", f"webhook_endpoints/{endpoint['id']}")  # its secret can't be read back: replace it
        endpoint = api("POST", "webhook_endpoints", {"url": url, "enabled_events": EVENTS, "description": "Flowly plans"})
        conf.set_value("STRIPE_WEBHOOK_SECRET", endpoint["secret"])
        done["webhook"] = endpoint["id"]
    return done


_price_cache: dict = {"at": 0.0, "data": None}


def plan_prices() -> dict | None:
    """{"pro": {"month": {"amount": 600, "currency": "eur", "tax": …}, "year": …}, "pro_plus": …} (cached an hour)."""
    if not enabled():
        return None
    if _price_cache["data"] is None or time.monotonic() - _price_cache["at"] > 3600:
        try:
            data = {}
            for plan, (prefix, *_rest) in PLANS.items():
                if not conf.get(f"{prefix}_MONTH"):
                    continue
                data[plan] = {}
                for interval in ("month", "year"):
                    price = api("GET", f"prices/{conf.get(f'{prefix}_{interval.upper()}')}")
                    data[plan][interval] = {"amount": price["unit_amount"], "currency": price["currency"],
                                            "tax": price.get("tax_behavior", "exclusive")}
            _price_cache.update(data=data, at=time.monotonic())
        except StripeError as exc:
            log.warning("Stripe prices: %s", exc)
            return None
    return _price_cache["data"]


def pro_prices() -> dict | None:
    return (plan_prices() or {}).get("pro")


# --- Pro (self-serve) -----------------------------------------------------------


def customer_for_user(user) -> Customer:
    existing = Customer.objects.filter(user=user).first()
    if existing:
        return existing
    data = api("POST", "customers", {"email": user.email, "name": user.full_name or user.email,
                                     "metadata": {"flowly_user": user.id}}, idempotency_key=f"flowly-user-{user.id}")
    return Customer.objects.create(user=user, customer_id=data["id"])


def checkout(user, interval: str, plan: str = "pro") -> str:
    if interval not in ("month", "year"):
        raise ValueError("interval must be month or year")
    if plan not in PLANS or not conf.get(f"{PLANS[plan][0]}_{interval.upper()}"):
        raise ValueError("Unknown plan.")
    customer = customer_for_user(user)
    app = conf.get("PUBLIC_URL")
    params = {
        "mode": "subscription",
        "customer": customer.customer_id,
        "line_items": [{"price": conf.get(f"{PLANS[plan][0]}_{interval.upper()}"), "quantity": 1}],
        "success_url": f"{app}/settings?pro=success#plan",
        "cancel_url": f"{app}/settings?pro=cancelled#plan",
        "allow_promotion_codes": True,
        "client_reference_id": str(user.id),
        "subscription_data": {"metadata": {"kind": plan, "flowly_user": user.id}},
        "customer_update": {"address": "auto", "name": "auto"},
        "tax_id_collection": {"enabled": True},
        "billing_address_collection": "required",
    }
    if conf.get("STRIPE_AUTOMATIC_TAX"):
        params["automatic_tax"] = {"enabled": True}
    return api("POST", "checkout/sessions", params)["url"]


def portal(user) -> str:
    customer = Customer.objects.filter(user=user).first()
    if customer is None:
        raise ValueError("No subscription to manage.")
    params = {"customer": customer.customer_id, "return_url": f"{conf.get('PUBLIC_URL')}/settings#plan"}
    if conf.get("STRIPE_PORTAL_CONFIG"):
        params["configuration"] = conf.get("STRIPE_PORTAL_CONFIG")
    return api("POST", "billing_portal/sessions", params)["url"]


# --- webhooks -------------------------------------------------------------------


def _ts(value):
    return datetime.fromtimestamp(value, tz=dt_timezone.utc) if value else None


@transaction.atomic
def sync(sub: dict) -> Subscription | None:
    """Store a Stripe subscription and apply it to the plan of its person or workspace."""
    customer = Customer.objects.filter(customer_id=sub["customer"]).select_related("user", "workspace").first()
    if customer is None:
        log.warning("Subscription %s for an unknown customer %s", sub["id"], sub["customer"])
        return None
    kind = (sub.get("metadata") or {}).get("kind") or ("enterprise" if customer.workspace_id else "pro")
    item = ((sub.get("items") or {}).get("data") or [{}])[0]
    price = item.get("price") or {}
    row, created = Subscription.objects.update_or_create(subscription_id=sub["id"], defaults={
        "kind": kind, "customer": customer, "status": sub["status"],
        "interval": (price.get("recurring") or {}).get("interval", ""), "amount": price.get("unit_amount") or 0,
        "currency": price.get("currency") or "eur", "current_period_end": _ts(stripe_api.period_end(sub)),
        "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
    })
    if row.live and row.kind == "pro_plus":
        # Upgrading from Pro: end the old Pro subscription now, with a prorated credit.
        for old in customer.subscriptions.filter(kind="pro").exclude(pk=row.pk):
            if old.live:
                try:
                    api("DELETE", f"subscriptions/{old.subscription_id}", {"prorate": True})
                    old.status = "canceled"
                    old.save(update_fields=["status"])
                except StripeError as exc:
                    log.warning("Could not cancel Pro %s after the Pro Plus upgrade: %s", old.subscription_id, exc)
    apply(customer)
    return row


def apply(customer: Customer):
    """The plan follows the best live subscription; when none is left, Stripe-granted plans end."""
    live = [s for s in customer.subscriptions.all() if s.live]
    best = max(live, key=lambda s: (RANK.get(s.kind, 0), s.current_period_end or timezone.now()), default=None)
    if customer.user_id:
        user = customer.user
        if best:
            user.plan, user.plan_source = (best.kind if best.kind in RANK else "pro"), "stripe"
            user.plan_expires_at = (best.current_period_end + GRACE) if best.current_period_end else None
        elif user.plan_source == "stripe":
            user.plan, user.plan_expires_at = "free", None
        user.save(update_fields=["plan", "plan_source", "plan_expires_at"])
    if customer.workspace_id:
        ws = customer.workspace
        if best:
            ws.plan, ws.plan_source = "enterprise", "stripe"
            ws.plan_expires_at = (best.current_period_end + GRACE) if best.current_period_end else None
        elif ws.plan_source == "stripe":
            ws.plan, ws.plan_expires_at = "standard", None
        ws.save(update_fields=["plan", "plan_source", "plan_expires_at"])


def handle_event(event: dict):
    kind, obj = event["type"], event["data"]["object"]
    if kind.startswith("customer.subscription."):
        sync(obj)
    elif kind == "checkout.session.completed" and obj.get("mode") == "subscription" and obj.get("subscription"):
        sync(api("GET", f"subscriptions/{obj['subscription']}"))
    elif kind == "invoice.payment_failed":
        customer = Customer.objects.filter(customer_id=obj.get("customer")).select_related("user").first()
        if customer and customer.user_id:
            from concierge import notify

            notify.create(customer.user, "assigned", "Your Flowly payment failed",
                          "Update your card in Settings → Plan → Manage subscription to keep Pro.")


# --- Enterprise (sales-led) -----------------------------------------------------


def enterprise_subscribe(workspace, *, amount_cents: int, interval: str, email: str, name: str = "", address: dict | None = None,
                         vat_id: str = "") -> Subscription:
    if interval not in ("month", "year") or amount_cents <= 0:
        raise ValueError("Give a positive amount and a monthly or yearly interval.")
    customer = Customer.objects.filter(workspace=workspace).first()
    params = {"email": email, "name": name or workspace.name, "metadata": {"flowly_workspace": workspace.id}}
    if address:
        params["address"] = {k: v for k, v in address.items() if v}
    if customer is None:
        if vat_id:
            params["tax_id_data"] = [{"type": "eu_vat", "value": vat_id}]
        data = api("POST", "customers", params)
        customer = Customer.objects.create(workspace=workspace, customer_id=data["id"])
    else:
        api("POST", f"customers/{customer.customer_id}", params)
    sub_params = {
        "customer": customer.customer_id,
        "items": [{"price_data": {"currency": "eur", "product": conf.get("STRIPE_PRODUCT_ENTERPRISE"), "unit_amount": amount_cents,
                                  "recurring": {"interval": interval}, "tax_behavior": "exclusive"}}],
        "collection_method": "send_invoice",
        "days_until_due": 30,
        "metadata": {"kind": "enterprise", "flowly_workspace": workspace.id},
    }
    if conf.get("STRIPE_AUTOMATIC_TAX") and address and address.get("country"):
        sub_params["automatic_tax"] = {"enabled": True}
    return sync(api("POST", "subscriptions", sub_params))


def enterprise_cancel(workspace) -> Subscription | None:
    row = Subscription.objects.filter(customer__workspace=workspace, kind="enterprise").exclude(status="canceled").first()
    if row is None:
        return None
    return sync(api("POST", f"subscriptions/{row.subscription_id}", {"cancel_at_period_end": True}))


# --- pay links on the user's invoices (their own Stripe account) ------------------


def user_key(profile) -> str:
    return decrypt(profile.stripe_key) if profile.stripe_key else ""


def connect_key(profile, raw: str):
    raw = (raw or "").strip()
    if not raw:
        profile.stripe_key, profile.stripe_account = "", ""
        return
    if not raw.startswith(("sk_", "rk_")):
        raise ValueError("Paste a Stripe secret key (sk_…) or restricted key (rk_…).")
    stripe_api.call(raw, "GET", "payment_links", {"limit": 1})  # needs Payment Links access
    try:
        account = stripe_api.call(raw, "GET", "account")
        name = (account.get("settings") or {}).get("dashboard", {}).get("display_name") or account.get("email") or account.get("id", "")
    except StripeError:
        name = "Stripe account"  # restricted keys may not read the account: fine
    profile.stripe_key, profile.stripe_account = encrypt(raw), name[:120]


def payment_link(invoice):
    """The invoice's pay link (created once, on the issuer's Stripe account)."""
    from timetracking.models import InvoicingProfile

    try:
        existing = invoice.payment
    except InvoicePayment.DoesNotExist:
        existing = None
    if existing:
        return existing
    profile = InvoicingProfile.for_user(invoice.created_by) if invoice.created_by_id else None
    secret = user_key(profile) if profile else ""
    if not secret:
        raise ValueError("Connect your Stripe account first (Settings → Invoicing).")
    cents = int((Decimal(invoice.total) * 100).quantize(Decimal(1)))
    if cents <= 0:
        raise ValueError("Nothing to pay on this invoice.")
    currency = invoice.currency.lower()
    price = stripe_api.call(secret, "POST", "prices", {
        "currency": currency, "unit_amount": cents,
        "product_data": {"name": f"Invoice {invoice.number}" + (f" · {invoice.client_name}" if invoice.client_name else "")},
    })
    link = stripe_api.call(secret, "POST", "payment_links", {
        "line_items": [{"price": price["id"], "quantity": 1}],
        "metadata": {"flowly_invoice": invoice.id, "number": invoice.number},
        "payment_intent_data": {"description": f"Invoice {invoice.number}", "metadata": {"flowly_invoice": invoice.id}},
        "after_completion": {"type": "hosted_confirmation",
                             "hosted_confirmation": {"custom_message": f"Thank you! Invoice {invoice.number} is paid."}},
    }, idempotency_key=f"flowly-invoice-link-{invoice.id}")
    return InvoicePayment.objects.create(invoice=invoice, payment_link_id=link["id"], url=link["url"], amount=cents, currency=currency)


def check(payment: InvoicePayment) -> bool:
    """Ask the issuer's Stripe account whether the link was paid; mark the invoice paid if so."""
    from timetracking.models import Invoice, InvoicingProfile

    if payment.paid_at:
        return True
    invoice = payment.invoice
    profile = InvoicingProfile.for_user(invoice.created_by) if invoice.created_by_id else None
    secret = user_key(profile) if profile else ""
    payment.checked_at = timezone.now()
    if not secret:
        payment.save(update_fields=["checked_at"])
        return False
    sessions = stripe_api.call(secret, "GET", "checkout/sessions", {"payment_link": payment.payment_link_id, "status": "complete", "limit": 5})
    paid = any(s.get("payment_status") == "paid" for s in sessions.get("data", []))
    if paid:
        payment.paid_at = timezone.now()
        if invoice.status != Invoice.Status.PAID:
            invoice.status = Invoice.Status.PAID
            invoice.save(update_fields=["status"])
        try:
            stripe_api.call(secret, "POST", f"payment_links/{payment.payment_link_id}", {"active": False})
        except StripeError:
            pass
        from concierge import notify

        if invoice.created_by_id:
            notify.create(invoice.created_by, "assigned", f"Invoice {invoice.number} was paid online",
                          f"{invoice.total} {invoice.currency} · {invoice.client_name}", workspace=invoice.workspace)
    payment.save(update_fields=["paid_at", "checked_at"])
    return paid


_last_check = 0.0


def tick():
    """Worker: check open pay links every 10 minutes."""
    global _last_check
    if time.monotonic() - _last_check < 60:
        return
    _last_check = time.monotonic()
    due = InvoicePayment.objects.filter(paid_at__isnull=True).filter(
        checked_at__isnull=True) | InvoicePayment.objects.filter(paid_at__isnull=True, checked_at__lt=timezone.now() - timedelta(minutes=10))
    for payment in due.select_related("invoice__created_by")[:20]:
        try:
            check(payment)
        except StripeError as exc:
            log.warning("Pay link %s check failed: %s", payment.pk, exc)
