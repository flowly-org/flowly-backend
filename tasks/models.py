import uuid

from django.conf import settings
from django.core.files.storage import Storage, storages
from django.core.validators import RegexValidator
from django.db import models

from workspaces.models import Workspace

hex_color = RegexValidator(r"^#[0-9a-fA-F]{6}$", "Use a hex color like #4f46e5.")


class Project(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="projects")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    color = models.CharField(max_length=7, default="#4f46e5", validators=[hex_color])
    archived = models.BooleanField(default=False)
    #: Off = no timers or logged time on this project's tasks.
    time_tracking = models.BooleanField(default=True)
    position = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "name"], name="unique_project_name")
        ]

    def __str__(self):
        return self.name


class TagGroup(models.Model):
    """A dimension (e.g. "Context", "Energy") whose tags can become swimlanes.

    A task carries at most one tag from each group. Every workspace also has two
    built-in groups: STATUS (its tags are the board columns, stored on Task.status)
    and PRIORITY (stored on Task.priority). They're edited like any other group but
    can't be deleted.
    """

    class Kind(models.TextChoices):
        CUSTOM = "custom", "Custom"
        STATUS = "status", "Status"
        PRIORITY = "priority", "Priority"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="tag_groups")
    name = models.CharField(max_length=60)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.CUSTOM)
    position = models.IntegerField(default=0)

    class Meta:
        ordering = ["position", "name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "name"], name="unique_tag_group_name"),
            models.UniqueConstraint(
                fields=["workspace", "kind"],
                condition=~models.Q(kind="custom"),
                name="unique_system_tag_group",
            ),
        ]

    @property
    def is_system(self):
        return self.kind != self.Kind.CUSTOM

    def __str__(self):
        return self.name


class Tag(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="tags")
    group = models.ForeignKey(
        TagGroup, null=True, blank=True, on_delete=models.CASCADE, related_name="tags"
    )
    name = models.CharField(max_length=60)
    color = models.CharField(max_length=7, default="#64748b", validators=[hex_color])
    position = models.IntegerField(default=0)
    is_done = models.BooleanField(
        default=False, help_text="Status tags only: tasks in this status count as finished."
    )

    class Meta:
        ordering = ["position", "name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "group", "name"], name="unique_tag_name")
        ]

    def __str__(self):
        return f"{self.group}: {self.name}" if self.group_id else self.name


REF_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"


def new_ref():
    import secrets

    return "".join(secrets.choice(REF_ALPHABET) for _ in range(10))


class Task(models.Model):
    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private"
        TEAM = "team", "Team"
        SHAREABLE = "shareable", "Shareable"
        PUBLIC = "public", "Public"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="tasks")
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks"
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    #: Stable public id used in links (/t/<ref>).
    ref = models.CharField(max_length=16, unique=True, default=new_ref, editable=False)
    #: private: creator + assignee · team: workspace members · shareable: team + people it's shared with
    #: · public: anyone with the link (read-only).
    visibility = models.CharField(max_length=10, choices=Visibility.choices, default=Visibility.TEAM)
    # RESTRICT (not PROTECT) so deleting a whole workspace cascades; deleting a single status
    # tag goes through TagViewSet, which first moves its tasks to another status.
    status = models.ForeignKey(
        Tag, on_delete=models.RESTRICT, related_name="status_tasks", help_text="A tag of the STATUS group."
    )
    priority = models.ForeignKey(
        Tag,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="priority_tasks",
        help_text="A tag of the PRIORITY group; empty = no priority.",
    )
    planned_date = models.DateField(
        null=True, blank=True, help_text="The day this task is planned for; empty = backlog."
    )
    due_date = models.DateField(null=True, blank=True)
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_tasks",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    tags = models.ManyToManyField(Tag, blank=True, related_name="tasks")
    #: Tickable items: [{"id": "k3f9", "text": "Buy paint", "done": false}, …].
    checklist = models.JSONField(default=list, blank=True)
    #: How long it should take, in minutes (for the day's load).
    estimate_minutes = models.PositiveIntegerField(null=True, blank=True)
    #: Repeat rule (see tasks.recurrence): {"freq": "weekly", "interval": 1, "weekdays": [0, 3], "after_done": false,
    #: "until": "2027-01-01"}. When the task is done, the next occurrence is created and takes the rule over.
    recurrence = models.JSONField(null=True, blank=True)
    #: Occurrences of one recurring task share this id.
    series = models.CharField(max_length=16, blank=True, db_index=True)
    position = models.FloatField(default=0, help_text="Sort key within a board cell.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["position", "id"]
        indexes = [
            models.Index(fields=["workspace", "planned_date"]),
            models.Index(fields=["workspace", "assignee", "planned_date"]),
        ]

    def __str__(self):
        return self.title


class Comment(models.Model):
    """A message in a task's discussion. Comments can also arrive from integrations (e.g. GitHub)."""

    class Source(models.TextChoices):
        FLOWLY = "flowly", "Flowly"
        GITHUB = "github", "GitHub"
        AI = "ai", "AI delegation"
        DONETICK = "donetick", "Donetick"
        IMPORT = "import", "Imported"

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    body = models.TextField()
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.FLOWLY)
    #: Display name for authors who aren't Flowly users (e.g. a GitHub login).
    external_author = models.CharField(max_length=100, blank=True)
    external_id = models.CharField(max_length=100, blank=True)
    external_url = models.URLField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]


_s3_cache: dict = {}


def s3_options():
    from instance import conf

    bucket = conf.get("S3_BUCKET")
    if not bucket:
        return None
    return {
        "bucket_name": bucket,
        "endpoint_url": conf.get("S3_ENDPOINT_URL") or None,
        "access_key": conf.get("S3_ACCESS_KEY_ID"),
        "secret_key": conf.get("S3_SECRET_ACCESS_KEY"),
        "region_name": conf.get("S3_REGION") or None,
        "addressing_style": conf.get("S3_ADDRESSING_STYLE") or None,
        "signature_version": "s3v4",
        "default_acl": None,
        "file_overwrite": False,
        "querystring_auth": True,
        "querystring_expire": 300,
    }


class AttachmentStorage(Storage):
    """Proxy to the attachments backend: the admin-configured S3 bucket (rebuilt when its settings change),
    or STORAGES["attachments"] when defined (tests, custom deployments)."""

    @property
    def backend(self):
        if "attachments" in settings.STORAGES:
            return storages["attachments"]
        options = s3_options()
        if options is None:
            raise RuntimeError("File uploads aren't configured.")
        key = tuple(sorted((k, str(v)) for k, v in options.items()))
        if key not in _s3_cache:
            from storages.backends.s3 import S3Storage

            _s3_cache.clear()
            _s3_cache[key] = S3Storage(**options)
        return _s3_cache[key]

    def save(self, name, content, max_length=None):
        return self.backend.save(name, content, max_length=max_length)

    def open(self, name, mode="rb"):
        return self.backend.open(name, mode)

    def delete(self, name):
        return self.backend.delete(name)

    def exists(self, name):
        return self.backend.exists(name)

    def size(self, name):
        return self.backend.size(name)

    def url(self, name):
        return self.backend.url(name)

    def generate_filename(self, filename):
        return self.backend.generate_filename(filename)

    def get_available_name(self, name, max_length=None):
        return self.backend.get_available_name(name, max_length=max_length)


def uploads_enabled():
    return "attachments" in settings.STORAGES or s3_options() is not None


def attachments_storage():
    return AttachmentStorage()


def attachment_path(instance, filename):
    return f"attachments/{instance.task.workspace_id}/{instance.task_id}/{uuid.uuid4().hex}/{filename}"


class Attachment(models.Model):
    """A document on a task: an uploaded file (S3) or a link to a cloud document."""

    class Kind(models.TextChoices):
        FILE = "file", "Uploaded file"
        LINK = "link", "Link"
        GOOGLE_DRIVE = "google_drive", "Google Drive"
        ONEDRIVE = "onedrive", "OneDrive / SharePoint"

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="attachments")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    name = models.CharField(max_length=255)
    url = models.URLField(max_length=2000, blank=True, help_text="For links and cloud documents.")
    file = models.FileField(upload_to=attachment_path, storage=attachments_storage, max_length=500, blank=True)
    size = models.BigIntegerField(null=True, blank=True)
    content_type = models.CharField(max_length=150, blank=True)
    #: Provider details (file id, icon URL, MIME type…).
    meta = models.JSONField(default=dict, blank=True)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class TaskShare(models.Model):
    """A person outside (or inside) the workspace a shareable task is shared with."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="shares")
    email = models.EmailField()
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="task_shares")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["task", "email"], name="unique_task_share")]
        ordering = ["created_at", "id"]


class Reminder(models.Model):
    """"Remind me at…" for one person on one task: a notification (and Telegram, push) at that moment."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="reminders")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reminders")
    remind_at = models.DateTimeField(db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["remind_at", "id"]


class TaskTemplate(models.Model):
    """A reusable set of tasks, written in the bulk-add format (with @+N days after the start, ~estimates, checklists)."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="task_templates")
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=300, blank=True)
    #: The tasks, one per line, in the bulk-add format.
    body = models.TextField()
    #: Project every task goes to unless its line says +Another.
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class TaskDependency(models.Model):
    """`task` can't really start (or finish) before `depends_on` is done."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="blocked_by_links")
    depends_on = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="blocking_links")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["task", "depends_on"], name="unique_task_dependency"),
            models.CheckConstraint(condition=~models.Q(task=models.F("depends_on")), name="no_self_dependency"),
        ]
