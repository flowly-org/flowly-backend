"""Postgres-backed delivery queue: pending outgoing deliveries are claimed with SKIP LOCKED and sent."""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .connectors import REGISTRY
from .http import DeliveryError
from .models import Delivery

log = logging.getLogger("flowly.worker")

# Delay before attempt n+1; after the last one the delivery is marked failed.
BACKOFF = [timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=10), timedelta(hours=1), timedelta(hours=6)]


def claim(limit=10):
    with transaction.atomic():
        due = list(
            Delivery.objects.select_for_update(skip_locked=True)
            .filter(direction=Delivery.Direction.OUT, status=Delivery.Status.PENDING, next_attempt_at__lte=timezone.now())
            .order_by("next_attempt_at", "id")[:limit]
        )
        # Push them out of reach of other workers while we work on them.
        Delivery.objects.filter(pk__in=[d.pk for d in due]).update(next_attempt_at=timezone.now() + timedelta(minutes=5))
    return due


def run_one(delivery: Delivery) -> Delivery:
    delivery = Delivery.objects.select_related("integration__workspace").get(pk=delivery.pk)
    connector = REGISTRY[delivery.integration.kind]
    delivery.attempts += 1
    try:
        result = connector.deliver(delivery)
    except DeliveryError as exc:
        delivery.last_error = str(exc)
        delivery.response_status = exc.status
        delivery.response_body = exc.body
        if exc.retry and delivery.attempts <= len(BACKOFF):
            delivery.next_attempt_at = timezone.now() + BACKOFF[delivery.attempts - 1]
        else:
            delivery.status = Delivery.Status.FAILED
            delivery.finished_at = timezone.now()
    except Exception as exc:  # a bug in a connector shouldn't kill the worker
        log.exception("Delivery %s crashed", delivery.pk)
        delivery.last_error = f"Internal error: {exc}"
        delivery.status = Delivery.Status.FAILED
        delivery.finished_at = timezone.now()
    else:
        delivery.status = Delivery.Status.SUCCESS
        delivery.finished_at = timezone.now()
        delivery.last_error = ""
        if isinstance(result, dict):
            delivery.response_status = result.get("status")
            delivery.response_body = str(result.get("body", ""))[:2000]
        elif result is not None:
            delivery.response_status = result.status_code
            delivery.response_body = result.text[:2000]
    delivery.save()
    return delivery


def process_due(limit=10) -> int:
    batch = claim(limit)
    for delivery in batch:
        run_one(delivery)
    return len(batch)
