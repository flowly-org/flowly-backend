from django.urls import path

from . import views

urlpatterns = [
    path("billing/plan/", views.PlanView.as_view(), name="billing-plan"),
    path("billing/checkout/", views.CheckoutView.as_view(), name="billing-checkout"),
    path("billing/portal/", views.PortalView.as_view(), name="billing-portal"),
    path("billing/stripe/webhook/", views.WebhookView.as_view(), name="stripe-webhook"),
    path("admin/stripe/setup/", views.AdminSetupView.as_view(), name="admin-stripe-setup"),
    path("admin/workspaces/<int:pk>/stripe/", views.AdminEnterpriseView.as_view(), name="admin-workspace-stripe"),
    path("workspaces/<int:ws>/invoices/<int:pk>/payment-link/", views.InvoicePaymentView.as_view(), name="invoice-payment-link"),
]
