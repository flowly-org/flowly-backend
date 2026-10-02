import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from calendars.sync import tick as calendar_tick
from concierge.jobs import tick as concierge_tick
from payments.services import tick as payments_tick
from delegation.engine import process as delegation_process
from integrations.worker import process_due


class Command(BaseCommand):
    help = "Send integration deliveries with retries, and run the concierge (Telegram alerts, polling, briefs) and calendar sync."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=float, default=2.0, help="Seconds to sleep when idle.")
        parser.add_argument("--once", action="store_true", help="Process what's due and exit.")

    def handle(self, *args, interval, once, **options):
        stopping = False

        def stop(*_):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        self.stdout.write("Flowly worker started.")
        while not stopping:
            close_old_connections()
            done = process_due()
            concierge_tick()
            calendar_tick()
            payments_tick()
            done += delegation_process()
            if once:
                break
            if not done:
                time.sleep(interval)
        self.stdout.write("Flowly worker stopped.")
