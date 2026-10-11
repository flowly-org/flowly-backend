"""Custom attributes of workspaces and projects, and how tasks use them.

- `{{key}}` in a task's title or notes is a placeholder: the value is put in when the task is shown (the stored text
  keeps the placeholder, so changing the attribute updates every task).
- `@key` points at an attribute without inserting it: shown as a chip, and passed to Flo and the delegation agent as
  context for that task.
The project's value wins over the workspace's for the same key. Unknown keys are left as they are.
"""

import re
from datetime import date

KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
PLACEHOLDER = re.compile(r"\{\{\s*([a-z][a-z0-9_]{0,39})\s*\}\}")
MENTION = re.compile(r"(?<![\w@/.])@([a-z][a-z0-9_]{0,39})\b")
#: Characters of a value used inside a title (placeholders in titles stay short).
TITLE_CHARS = 120


def clean_key(raw: str) -> str:
    """"Client e-mail" → "client_e_mail"; refuses words that bulk-add reads as dates (@today, @friday…)."""
    from .bulk import parse_date

    key = re.sub(r"[^a-z0-9]+", "_", (raw or "").strip().lower()).strip("_")[:40]
    if not KEY.match(key):
        raise ValueError("Use a short name that starts with a letter, e.g. client_email.")
    if parse_date(key, date.today()) is not ...:
        raise ValueError(f"“{key}” reads as a date in task lists (@{key}): pick another name.")
    return key


class Values:
    """Attribute values per (workspace, project), loaded once per workspace (one query, however many tasks)."""

    def __init__(self):
        self._by_ws: dict[int, list] = {}

    def _rows(self, workspace_id):
        from .models import Attribute

        if workspace_id not in self._by_ws:
            self._by_ws[workspace_id] = list(Attribute.objects.filter(workspace_id=workspace_id).values("project_id", "key", "value"))
        return self._by_ws[workspace_id]

    def for_task(self, workspace_id, project_id) -> dict[str, str]:
        rows = self._rows(workspace_id)
        values = {r["key"]: r["value"] for r in rows if r["project_id"] is None}
        values.update({r["key"]: r["value"] for r in rows if project_id and r["project_id"] == project_id})
        return values


def render(text: str, values: dict, limit: int | None = None) -> str:
    """Puts the values in place of {{key}}. With `limit` (titles), values are flattened to one line and shortened."""
    if not text or "{{" not in text:
        return text

    def put(m):
        value = values.get(m.group(1))
        if value is None:
            return m.group(0)
        if limit:
            value = " ".join(value.split())
            value = value[:limit] + ("…" if len(value) > limit else "")
        return value

    return PLACEHOLDER.sub(put, text)


def mentioned(text: str, values: dict) -> dict[str, str]:
    """The attributes a text points at with @key (in order, once each)."""
    out = {}
    for key in MENTION.findall(text or ""):
        if key in values and key not in out:
            out[key] = values[key]
    return out


def for_task(task, cache: Values | None = None) -> dict[str, str]:
    return (cache or Values()).for_task(task.workspace_id, task.project_id)


def rendered_title(task, cache: Values | None = None) -> str:
    return render(task.title, for_task(task, cache), TITLE_CHARS) if "{{" in task.title else task.title


def context(task, cache: Values | None = None) -> str:
    """For Flo and the agent: the attributes the task uses, with their full values."""
    values = for_task(task, cache)
    used = {k: values[k] for k in PLACEHOLDER.findall(f"{task.title}\n{task.description}") if k in values}
    used.update(mentioned(f"{task.title}\n{task.description}", values))
    return "\n".join(f"- {k}: {v}" for k, v in used.items())


def summary(workspace_id, project_id=None, limit=160) -> str:
    """"key=value; …" for Flo's overview of workspaces and projects (values shortened)."""
    from .models import Attribute

    qs = Attribute.objects.filter(workspace_id=workspace_id, project_id=project_id)
    parts = [f"{a.key}={' '.join(a.value.split())[:limit]}" for a in qs]
    return "; ".join(parts)
