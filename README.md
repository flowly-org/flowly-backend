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

## Integrations

Integrations belong to a workspace and can be limited to one project (`project`). Owners and admins
manage them at `workspaces/{ws}/integrations/` (CRUD, plus `test/`, `rotate-secret/`, `deliveries/`,
`deliveries/{id}/retry/`, `github/install/`, `github/repos/`).

| Kind | Direction | What it does |
|---|---|---|
| `github` | both | Issues of one repo ↔ tasks. Opened/edited/closed/reopened/assigned/labelled issues create or update the linked task; moving a task into/out of a "done" status closes/reopens the issue. Optional: mirror the status as a `flowly: <status>` label, open issues for tasks created in Flowly. Assignees are matched by each user's `github_login`. |
| `slack` | out | Posts task events to a channel through a Slack incoming-webhook URL. |
| `outgoing_webhook` | out | POSTs JSON to your URL; header `X-Flowly-Signature: sha256=<HMAC-SHA256(secret, body)>`. |
| `incoming_webhook` | in | `POST /api/hooks/<token>/` with `{external_id?, title, description, status, priority, tags[], assignee (email), planned_date, due_date, url}`; same `external_id` updates the task. |
| `email` | in | Mail to `<token>@EMAIL_INBOUND_DOMAIN` becomes a task (members only by default). Point your provider at `POST /api/integrations/email/inbound/` (JSON `{to, from, subject, text, message_id}`, Mailgun, SendGrid or Postmark formats; optional `?key=EMAIL_INBOUND_KEY`). |

Task changes (`task.created/updated/status_changed/completed/assigned/deleted`) are written to the
`Delivery` table and sent by the **worker** service (`python manage.py run_worker`) with retries
(30s, 2m, 10m, 1h, 6h; 4xx responses other than 408/409/429 aren't retried). A change coming from an
integration is never echoed back to that same integration. Outgoing URLs that resolve to private
addresses are refused unless `INTEGRATIONS_ALLOW_PRIVATE_URLS=1`.

### GitHub App

Flowly talks to GitHub through one GitHub App per instance, created with GitHub's manifest flow:
an instance admin (`is_staff`) opens Integrations → GitHub → **Create GitHub App**, confirms on GitHub
and is sent back; credentials are stored in the `GitHubApp` table. Set `PUBLIC_URL` first: GitHub
uses it for the webhook (`<PUBLIC_URL>/api/integrations/github/webhook/`) and redirect URLs.
Each GitHub integration is then installed on an account/repos via **Install on GitHub**.

## Discussion & attachments

`workspaces/{ws}/tasks/{task}/comments/` (any member can comment; authors edit/delete their own,
admins can delete any) and `…/attachments/` (`kind`: `file` multipart upload, `link`, `google_drive`,
`onedrive`; `GET …/attachments/{id}/download/` returns a short-lived URL). Comments on tasks linked to
GitHub issues sync both ways. `GET /api/config/` tells the client which sources are enabled.

| Setting | Enables |
|---|---|
| `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION`, `S3_ADDRESSING_STYLE` | File uploads to any S3-compatible bucket (private; downloads use 5-minute signed URLs, so the endpoint must be reachable by browsers). `ATTACHMENT_MAX_MB` (default 25). |
| `GOOGLE_CLIENT_ID`, `GOOGLE_API_KEY`, `GOOGLE_APP_ID` | Google Drive picker (OAuth client of type *Web*, authorised JS origin = your site; Picker API enabled; app id = project number). Scope `drive.file`; Flowly stores only links. |
| `MICROSOFT_CLIENT_ID` | OneDrive / SharePoint picker (Entra app registration, SPA platform with your site's origin + `/` as redirect URI). |

## Notifications & concierge

Every user gets in-app notifications (`GET/POST /api/notifications/`): a task assigned to you by
someone else, an **urgent** task (top priority of its workspace) landing on your today, and comments
on your tasks. The web app polls them and refreshes open boards.

The **concierge** is a Pro feature (`User.plan = "pro"`, set in `/admin`; staff always have it): a
personal assistant on Telegram and in Settings that sends a morning brief, forwards the alerts above,
and answers/acts on requests with tools over your tasks (list/create/update/complete/comment/remember).

| Setting | Purpose |
|---|---|
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` (default `openrouter/auto`; pick a tool-calling model) | Platform LLM for Pro users. `CONCIERGE_DAILY_TOKENS` caps usage per user per day. |
| `TELEGRAM_BOT_TOKEN` | Bot created with @BotFather. Users link from Settings → Concierge (deep link `t.me/<bot>?start=<code>`). |
| `TELEGRAM_MODE` | `polling` (default, done by the worker, no public URL needed) or `webhook`: set `API_PUBLIC_URL=https://api.flowly.net` and `TELEGRAM_WEBHOOK_SECRET`, then run `python manage.py telegram_webhook`. |

Workspaces can add an **OpenRouter** or **OpenAI-compatible** integration (API key stored encrypted).
A user may choose it as their concierge model; when a workspace's provider is marked *exclusive*,
that workspace's tasks are never sent to any other model.
