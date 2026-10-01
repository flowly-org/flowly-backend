from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import BillingView, InvoiceViewSet, ProjectBillingView, TimeEntryViewSet

router = SimpleRouter()
router.register("time-entries", TimeEntryViewSet, basename="time-entry")
router.register("invoices", InvoiceViewSet, basename="invoice")

urlpatterns = [
    path("billing/", BillingView.as_view(), name="billing"),
    path("billing/projects/<int:project_pk>/", ProjectBillingView.as_view(), name="project-billing"),
    *router.urls,
]
