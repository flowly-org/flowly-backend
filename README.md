# Flowly – backend

REST API for Flowly: multi-user, multi-project daily task planning where a day's tasks
are shown as a kanban board split into **swimlanes** by project, assignee or any tag group you define.

Django 5.2 · Django REST Framework · SimpleJWT · PostgreSQL · drf-spectacular (OpenAPI)

The Angular client lives in the separate `flowly-frontend` repository.

## Run it

```bash
docker compose up -d --build          # API on http://localhost:8000, Postgres in a volume
docker compose exec api python manage.py createsuperuser   # optional, for /admin
```

Useful env vars (see `.env.example`): `API_PORT` (host port, default 8000), `DJANGO_SECRET_KEY`,
`DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `DATABASE_URL`, `CORS_ALLOWED_ORIGINS`
(must include the frontend's origin, e.g. `http://localhost:8080`).

Run the tests (SQLite, no database container needed):

```bash
docker build -t flowly-backend . && docker run --rm --entrypoint python flowly-backend manage.py test
```

Without Docker: Python 3.12+, `pip install -r requirements.txt`, `python manage.py migrate && python manage.py runserver`
(falls back to SQLite when `DATABASE_URL` is unset).

API docs: http://localhost:8000/api/docs/ (Swagger) · schema at `/api/schema/`.

## Domain model

| Model | Notes |
|---|---|
| `User` | Email login, `full_name`. Signing up creates a "Personal" workspace and accepts pending invitations. |
| `Workspace` / `Membership` | Roles: `owner` (one, can delete), `admin` (manage members, rename), `member`. |
| `Invitation` | Created when you invite an email without an account; consumed on sign-up. |
| `Project` | Colored, orderable, archivable. Default swimlanes. |
| `TagGroup` | A dimension you plan by ("Context", "Energy"...). Its tags can be used as swimlanes. A task holds **at most one tag per group**. |
| `Tag` | Belongs to a group, or is a free label (`group = null`). |
| `Task` | `status` (todo / in_progress / done), `priority`, `project`, `assignee`, `tags`, `planned_date` (the day it's planned for, null = backlog), `due_date`, `position` (float sort key inside a board cell). |

## Endpoints

All under `/api/`, JWT bearer auth (`Authorization: Bearer <access>`).

```
POST   auth/register/                 {email, password, full_name} → {user, access, refresh}
POST   auth/login/                    {email, password} → {access, refresh}
POST   auth/refresh/                  {refresh} → {access, refresh}
GET    auth/me/  PATCH auth/me/

GET    workspaces/                    yours, with my_role and member_count
POST   workspaces/  PATCH/DELETE workspaces/{id}/

GET    workspaces/{ws}/members/
POST   workspaces/{ws}/members/       {email, role} → 201 member added, or 202 invitation recorded
PATCH  workspaces/{ws}/members/{id}/  {role}
DELETE workspaces/{ws}/members/{id}/  remove someone, or leave (their tasks become unassigned)
GET    workspaces/{ws}/invitations/   DELETE workspaces/{ws}/invitations/{id}/

CRUD   workspaces/{ws}/projects/      ?archived=true|false
CRUD   workspaces/{ws}/tag-groups/    (includes nested tags)
CRUD   workspaces/{ws}/tags/          ?group={id}
CRUD   workspaces/{ws}/tasks/
```

Task filters:

| Param | Meaning |
|---|---|
| `day=YYYY-MM-DD` | planned for that day |
| `include_overdue=true` | with `day`: also unfinished tasks planned earlier (carry-over) |
| `unscheduled=true` | backlog (no planned date) |
| `date_from`, `date_to` | planned date range |
| `assignee=me\|none\|<id>` | |
| `project=none\|<id>`, `tag=<id>`, `status=todo&status=done`, `q=text` | |

Moving a card on the board is a plain `PATCH` of `status`, `position`, `planned_date` and the lane field
(`project`, `assignee` or `tags`).

Non-members get `404` for anything under a workspace. Related objects (projects, tags, assignees) are
validated to belong to the same workspace.
