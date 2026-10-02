"""Stripe: Flowly's own subscriptions (Pro per person, Enterprise per workspace) and pay links on users' invoices."""

from django.conf import settings
from django.db import models


class Customer(models.Model):
    """A Stripe customer on Flowly's account: a person (Pro) or a workspace (Enterprise)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="stripe_customer")
    workspace = models.OneToOneField("workspaces.Workspace", null=True, blank=True, on_delete=models.CASCADE, related_name="stripe_customer")
    customer_id = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Subscription(models.Model):
    class Kind(models.TextChoices):
        PRO = "pro", "Pro"
        ENTERPRISE = "enterprise", "Enterprise"

    kind = models.CharField(max_length=12, choices=Kind.choices)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="subscriptions")
    subscription_id = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=24)
    interval = models.CharField(max_length=10, blank=True)
    amount = models.PositiveIntegerField(default=0, help_text="Cents per interval, before tax.")
    currency = models.CharField(max_length=3, default="eur")
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    #: Statuses that keep the plan on (past_due: Stripe is still retrying the card).
    LIVE = ("active", "trialing", "past_due")

    @property
    def live(self) -> bool:
        return self.status in self.LIVE


class InvoicePayment(models.Model):
    """A Stripe payment link for one of a user's invoices, on the user's own Stripe account."""

    invoice = models.OneToOneField("timetracking.Invoice", on_delete=models.CASCADE, related_name="payment")
    payment_link_id = models.CharField(max_length=64)
    url = models.URLField(max_length=500)
    amount = models.PositiveIntegerField(help_text="Cents.")
    currency = models.CharField(max_length=3)
    paid_at = models.DateTimeField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
