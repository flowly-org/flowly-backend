"""Asana: a project's CSV export, or the API with a personal access token (adds comments and subtasks)."""

import csv
import io
import re

from integrations.http import DeliveryError, send

from .engine import parse_date

API = "https://app.asana.com/api/1.0"


def read_csv(text: str, filename="Asana") -> tuple[str, str, list[dict]]:
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows or "Name" not in rows[0] or "Task ID" not in rows[0]:
        raise ValueError("That isn't an Asana CSV export (it needs the Task ID and Name columns).")
    by_id = {r["Task ID"]: r for r in rows}
    items, subtasks = [], {}
    for r in rows:
        parent = (r.get("Parent task") or r.get("Parent Task") or "").strip()
        if parent:
            subtasks.setdefault(parent, []).append({"text": r["Name"], "done": bool(r.get("Completed At"))})
    for r in rows:
        if (r.get("Parent task") or r.get("Parent Task") or "").strip():
            continue
        assignee = None
        if r.get("Assignee") or r.get("Assignee Email"):
            assignee = {"name": r.get("Assignee", ""), "email": r.get("Assignee Email", "")}
        items.append({
            "id": r["Task ID"], "title": r["Name"], "notes": r.get("Notes", ""), "done": bool(r.get("Completed At")),
            "due": parse_date(r.get("Due Date")), "start": parse_date(r.get("Start Date")),
            "list": r.get("Section/Column", ""), "labels": [{"name": t.strip()} for t in (r.get("Tags") or "").split(",") if t.strip()],
            "checklist": subtasks.get(r["Name"], []) + subtasks.get(r["Task ID"], []), "assignee": assignee, "comments": [],
        })
    project = (rows[0].get("Projects") or filename).split(",")[0].strip() or filename
    return f"csv:{project}", project, items


def project_gid(value: str) -> str:
    """A project gid from a link like https://app.asana.com/0/1205/list or …/project/1205/list, or the gid itself."""
    value = value.strip()
    m = re.search(r"/project/(\d+)", value) or re.search(r"asana\.com/0/(\d+)", value) or re.fullmatch(r"(\d+)", value)
    if not m:
        raise ValueError("Paste the project's link from Asana (or its id).")
    return m.group(1)


def _get(token, path, params=None):
    from urllib.parse import urlencode

    url = f"{API}{path}" + ("?" + urlencode(params) if params else "")
    try:
        return send("GET", url, headers={"Authorization": f"Bearer {token}"}, timeout=30, check_url=False).json()
    except DeliveryError as exc:
        if exc.status in (401, 403):
            raise ValueError("Asana refused the token: create a personal access token in Asana → Settings → Apps → Developer apps.") from exc
        if exc.status == 404:
            raise ValueError("Asana couldn't find that project with this token.") from exc
        raise


def _paged(token, path, params):
    out, offset = [], None
    for _ in range(50):
        data = _get(token, path, {**params, "limit": 100, **({"offset": offset} if offset else {})})
        out += data.get("data", [])
        offset = (data.get("next_page") or {}).get("offset")
        if not offset:
            break
    return out


def read_api(token: str, project: str) -> tuple[str, str, list[dict]]:
    gid = project_gid(project)
    info = _get(token, f"/projects/{gid}", {"opt_fields": "name"}).get("data", {})
    fields = "name,notes,completed,due_on,start_on,assignee.name,assignee.email,memberships.section.name,memberships.project.gid,tags.name,num_subtasks"
    tasks = _paged(token, f"/projects/{gid}/tasks", {"opt_fields": fields})
    items = []
    for t in tasks[:3000]:
        section = next((m.get("section", {}).get("name", "") for m in t.get("memberships", []) if (m.get("project") or {}).get("gid") == gid), "")
        checklist = []
        if t.get("num_subtasks"):
            checklist = [{"text": s["name"], "done": s.get("completed", False)}
                         for s in _paged(token, f"/tasks/{t['gid']}/subtasks", {"opt_fields": "name,completed"})]
        stories = _paged(token, f"/tasks/{t['gid']}/stories", {"opt_fields": "type,text,created_by.name,created_at"})
        comments = [{"author": (s.get("created_by") or {}).get("name"), "text": s.get("text", ""), "date": s.get("created_at")}
                    for s in stories if s.get("type") == "comment"]
        assignee = {"name": t["assignee"].get("name", ""), "email": t["assignee"].get("email", "")} if t.get("assignee") else None
        items.append({
            "id": t["gid"], "title": t.get("name", ""), "notes": t.get("notes", ""), "done": t.get("completed", False),
            "due": parse_date(t.get("due_on")), "start": parse_date(t.get("start_on")), "list": section,
            "labels": [{"name": tag["name"]} for tag in t.get("tags", []) if tag.get("name")],
            "checklist": checklist, "assignee": assignee, "comments": comments,
        })
    return gid, info.get("name", "Asana project"), items
