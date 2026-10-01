from django.core.management.base import BaseCommand, CommandError

from concierge import telegram

from instance import conf


class Command(BaseCommand):
    help = "Register (or remove with --delete) the Telegram webhook at <API_PUBLIC_URL>/api/concierge/telegram/webhook/."

    def add_arguments(self, parser):
        parser.add_argument("--delete", action="store_true")

    def handle(self, *args, delete, **options):
        if not telegram.enabled():
            raise CommandError("TELEGRAM_BOT_TOKEN is not set.")
        if delete:
            telegram.delete_webhook()
            self.stdout.write("Webhook removed; use TELEGRAM_MODE=polling.")
            return
        if not conf.get("API_PUBLIC_URL").startswith("https://") or not conf.get("TELEGRAM_WEBHOOK_SECRET"):
            raise CommandError("Needs an https API_PUBLIC_URL and TELEGRAM_WEBHOOK_SECRET.")
        url = f"{conf.get("API_PUBLIC_URL")}/api/concierge/telegram/webhook/"
        telegram.set_webhook(url, conf.get("TELEGRAM_WEBHOOK_SECRET"))
        self.stdout.write(f"Webhook set to {url}. Set TELEGRAM_MODE=webhook.")
