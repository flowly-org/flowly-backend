import json
import logging

from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from instance import conf

from . import services, stripe_api
from .models import Subscription
from .stripe_api import StripeError

log = logging.getLogger("flowly.payments")


def sub_data(sub: Subscription | None):
    if sub is None:
        return None
    return {"status": sub.status, "interval": sub.interval, "amount": sub.amount, "currency": sub.currency,
            "current_period_end": sub.current_period_end, "cancel_at_period_end": sub.cancel_at_period_end}


class PlanView(APIView):
    """GET → the user's plan, the Pro prices and their Stripe subscription (if any)."""

    def get(self, request):
        user = request.user
        sub = (Subscription.objects.filter(customer__user=user, kind__in=["pro", "pro_plus"], status__in=Subscription.LIVE)
               .order_by("-updated_at").first()
               or Subscription.objects.filter(customer__user=user, kind__in=["pro", "pro_plus"]).order_by("-updated_at").first())
        prices = services.plan_prices()
        return Response({
            "stripe": services.enabled(),
            "prices": (prices or {}).get("pro"),
            "plans": prices,
            "plan": user.plan, "has_pro": user.has_pro, "has_pro_plus": user.has_pro_plus, "source": user.plan_source,
            "expires_at": user.plan_expires_at,
            "subscription": sub_data(sub),
            "can_manage": sub is not None,
        })


class CheckoutView(APIView):
    def post(self, request):
        if not services.enabled():
            return Response({"detail": "Payments aren't set up on this server yet."}, status=400)
        plan = request.data.get("plan", "pro")
        live = Subscription.objects.filter(customer__user=request.user, status__in=Subscription.LIVE)
        if live.filter(kind=plan).exists() or (plan == "pro" and live.filter(kind="pro_plus").exists()):
            return Response({"detail": "You already have this plan: manage it from Settings → Plan."}, status=400)
        try:
            return Response({"url": services.checkout(request.user, request.data.get("interval", "month"), plan)})
        except (ValueError, StripeError) as exc:
            return Response({"detail": str(exc)}, status=400)


class PortalView(APIView):
    def post(self, request):
        try:
            return Response({"url": services.portal(request.user)})
        except (ValueError, StripeError) as exc:
            return Response({"detail": str(exc)}, status=400)


class WebhookView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        if not stripe_api.verify_signature(request.body, request.headers.get("Stripe-Signature", ""), conf.get("STRIPE_WEBHOOK_SECRET")):
            return Response(status=status.HTTP_400_BAD_REQUEST)
        event = json.loads(request.body)
        try:
            services.handle_event(event)
        except StripeError as exc:
            log.warning("Stripe event %s failed: %s", event.get("id"), exc)
            return Response(status=status.HTTP_502_BAD_GATEWAY)  # Stripe retries
        return Response({"received": True})


class AdminSetupView(APIView):
    def post(self, request):
        if not request.user.is_staff:
            return Response(status=status.HTTP_403_FORBIDDEN)
        try:
            created = services.setup()
        except StripeError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response({"created": created, "enabled": services.enabled()})


class AdminEnterpriseView(APIView):
    """POST: start an invoiced Enterprise subscription for a workspace. DELETE: cancel it at period end."""

    def post(self, request, pk):
        from workspaces.models import Workspace

        if not request.user.is_staff:
            return Response(status=status.HTTP_403_FORBIDDEN)
        ws = get_object_or_404(Workspace, pk=pk)
        d = request.data
        try:
            amount = int(round(float(d.get("amount", 0)) * 100))
            sub = services.enterprise_subscribe(
                ws, amount_cents=amount, interval=d.get("interval", "year"), email=(d.get("email") or "").strip(),
                name=(d.get("name") or "").strip(), vat_id=(d.get("vat_id") or "").strip(),
                address={"line1": d.get("line1", ""), "postal_code": d.get("postal_code", ""), "city": d.get("city", ""),
                         "country": (d.get("country") or "").upper()},
            )
        except (ValueError, StripeError) as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(sub_data(sub), status=201)

    def delete(self, request, pk):
        from workspaces.models import Workspace

        if not request.user.is_staff:
            return Response(status=status.HTTP_403_FORBIDDEN)
        try:
            sub = services.enterprise_cancel(get_object_or_404(Workspace, pk=pk))
        except StripeError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(sub_data(sub))


class InvoicePaymentView(APIView):
    """POST: create (once) the invoice's "Pay online" link. GET: its state, checking Stripe."""

    def _invoice(self, request, ws, pk):
        from timetracking.models import Invoice
        from workspaces.models import Membership

        membership = Membership.objects.filter(workspace_id=ws, user=request.user).first()
        if membership is None or not membership.can_manage:
            return None
        return Invoice.objects.filter(workspace_id=ws, pk=pk).select_related("created_by").first()

    def post(self, request, ws, pk):
        invoice = self._invoice(request, ws, pk)
        if invoice is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if not request.user.has_pro:
            return Response({"detail": "Invoices are part of Flowly Pro."}, status=status.HTTP_402_PAYMENT_REQUIRED)
        if invoice.status in ("void", "paid"):
            return Response({"detail": "This invoice is already settled."}, status=400)
        try:
            payment = services.payment_link(invoice)
        except (ValueError, StripeError) as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response({"url": payment.url, "paid_at": payment.paid_at}, status=201)

    def get(self, request, ws, pk):
        from .models import InvoicePayment

        invoice = self._invoice(request, ws, pk)
        payment = InvoicePayment.objects.filter(invoice=invoice).first() if invoice else None
        if payment is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            services.check(payment)
        except StripeError as exc:
            log.warning("Pay link check failed: %s", exc)
        return Response({"url": payment.url, "paid_at": payment.paid_at, "invoice_status": payment.invoice.status})
