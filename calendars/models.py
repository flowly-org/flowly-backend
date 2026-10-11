"""External calendars (Google, Microsoft 365, CalDAV, iCal links) mirrored as read-only background for a user's days.

Events are not tasks. Most are just context; a few need preparation (tickets, travel, a trip away from
home). Those are flagged by `prep.classify` and checked by the concierge in the morning brief.
"""

from django.conf import settings
from django.db import models


class CalendarAccount(models.Model):
    class Provider(models.TextChoices):
        GOOGLE = "google", "Google Calendar"
        MICROSOFT = "microsoft", "Outlook / Microsoft 365"
        CALDAV = "caldav", "CalDAV"
        ICS = "ics", "iCal link"

    class Status(models.TextChoices):
        OK = "ok", "Connected"
        ERROR = "error", "Needs attention"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="calendar_accounts")
    provider = models.CharField(max_length=20, choices=Provider.choices, default=Provider.GOOGLE)
    email = models.EmailField(blank=True)
    #: CalDAV: the server (or calendar) address; iCal link: the feed address.
    server_url = models.CharField(max_length=500, blank=True)
    #: CalDAV login (the password, encrypted, is kept in refresh_token).
    username = models.CharField(max_length=200, blank=True)
    #: Encrypted with integrations.crypto.
    refresh_token = models.TextField(blank=True)
    access_token = models.TextField(blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OK)
    last_error = models.CharField(max_length=500, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "provider", "email", "server_url", "username"], name="unique_calendar_account_v2")]

    def __str__(self):
        return f"{self.get_provider_display()} {self.email or self.username or self.server_url}"


class Calendar(models.Model):
    account = models.ForeignKey(CalendarAccount, on_delete=models.CASCADE, related_name="calendars")
    external_id = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    color = models.CharField(max_length=20, blank=True)
    primary = models.BooleanField(default=False)
    #: Only selected calendars are synced and shown.
    selected = models.BooleanField(default=True)

    class Meta:
        ordering = ["-primary", "name"]
        constraints = [models.UniqueConstraint(fields=["account", "external_id"], name="unique_calendar")]


class CalendarEvent(models.Model):
    class Prep(models.TextChoices):
        AUTO = "auto", "Decide automatically"
        NEEDED = "needed", "Needs preparation"
        BACKGROUND = "background", "Just background"

    calendar = models.ForeignKey(Calendar, on_delete=models.CASCADE, related_name="events")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="calendar_events")
    external_id = models.CharField(max_length=255)
    title = models.CharField(max_length=500, blank=True)
    description = models.TextField(blank=True)
    location = models.CharField(max_length=500, blank=True)
    html_link = models.URLField(max_length=1000, blank=True)
    all_day = models.BooleanField(default=False)
    #: Timed events: aware datetimes. All-day events: midnight UTC of start date / exclusive end date.
    start = models.DateTimeField()
    end = models.DateTimeField()
    #: Local calendar dates covered (inclusive), for quick per-day lookups.
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(db_index=True)
    #: Heuristic verdict: "" (background) or a reason code from prep.REASONS.
    auto_reason = models.CharField(max_length=20, blank=True)
    #: The user's (or concierge's) decision, overriding the heuristic.
    prep = models.CharField(max_length=12, choices=Prep.choices, default=Prep.AUTO)
    #: Set when the user confirmed the event is still on (asked for events away from home).
    confirmed_at = models.DateTimeField(null=True, blank=True)
    #: Real driving time from home (opt-in, see calendars.routing): minutes, or empty for "no route / not yet".
    drive_minutes = models.PositiveIntegerField(null=True, blank=True)
    #: What drive_minutes was computed for (home + location); a change means computing it again.
    drive_key = models.CharField(max_length=20, blank=True)
    tasks = models.ManyToManyField("tasks.Task", blank=True, related_name="calendar_events")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start", "id"]
        constraints = [models.UniqueConstraint(fields=["calendar", "external_id"], name="unique_calendar_event")]
        indexes = [models.Index(fields=["user", "start_date", "end_date"])]

    @property
    def needs_prep(self) -> bool:
        if self.prep == self.Prep.AUTO:
            return bool(self.auto_reason)
        return self.prep == self.Prep.NEEDED

    @property
    def reason(self) -> str:
        if self.prep == self.Prep.BACKGROUND:
            return ""
        return self.auto_reason or ("manual" if self.prep == self.Prep.NEEDED else "")


class TimeBlock(models.Model):
    """Time set aside on a day for a task (or anything): shown on the day's timeline and in the iCal feed."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="time_blocks")
    task = models.ForeignKey("tasks.Task", null=True, blank=True, on_delete=models.CASCADE, related_name="time_blocks")
    title = models.CharField(max_length=255, blank=True)
    start = models.DateTimeField(db_index=True)
    end = models.DateTimeField()
    #: The start reminder went out.
    reminded = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start", "id"]


class GeoPlace(models.Model):
    """A geocoded address (Nominatim), cached because the public server asks for it. Empty lat = not found."""

    query = models.CharField(max_length=500, unique=True)
    lat = models.FloatField(null=True, blank=True)
    lon = models.FloatField(null=True, blank=True)
    checked_at = models.DateTimeField()
