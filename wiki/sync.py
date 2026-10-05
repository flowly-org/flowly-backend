"""Fetch a wiki by cloning it (shallow) and keep its text pages in the database.

Works for GitHub wikis (<repo>.wiki.git), a folder of a GitHub repository, any https git repository, or a MediaWiki
(read through its API, see mediawiki.py).
"""

import base64
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from integrations.crypto import decrypt
from integrations.http import DeliveryError, ensure_public_url

from .models import WikiPage, WikiSource

log = logging.getLogger("flowly.wiki")
EXTENSIONS = {".md", ".markdown", ".mdown", ".txt", ".rst", ".org", ".adoc", ".asciidoc", ".mediawiki", ".wiki", ".textile"}
MAX_PAGES = 500
MAX_PAGE_BYTES = 200_000
MAX_TOTAL_BYTES = 5_000_000
#: How often the worker refreshes each wiki.
EVERY = timezone.timedelta(hours=6)


def github_web() -> str:
    return getattr(settings, "GITHUB_WEB_URL", "https://github.com").rstrip("/")


def clone_url(config) -> str:
    source = config.get("source", "github_wiki")
    if source == "git":
        return config["url"]
    suffix = ".wiki.git" if source == "github_wiki" else ".git"
    return f"{github_web()}/{config['repo']}{suffix}"


def page_url(config, rel: str) -> str:
    source = config.get("source", "github_wiki")
    if source == "github_wiki":
        return f"{github_web()}/{config['repo']}/wiki/{quote(Path(rel).stem)}"
    if source == "github_repo":
        return f"{github_web()}/{config['repo']}/blob/{quote(config.get('branch') or 'HEAD')}/{quote(rel)}"
    return ""


def title_of(rel: str, text: str, source: str) -> str:
    if source != "github_wiki":
        for line in text.splitlines()[:20]:
            if line.startswith("# "):
                return line[2:].strip()[:300]
    name = Path(rel).stem
    return (name.replace("-", " ") if source == "github_wiki" else name)[:300] or rel[:300]


def git(args, cwd=None, token=""):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp", "GIT_TERMINAL_PROMPT": "0"}
    if token:
        # The token travels in the environment, never on the command line or in the URL.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader", GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")
    try:
        res = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired as exc:
        raise DeliveryError("The wiki took too long to download.") from exc
    if res.returncode:
        message = (res.stderr or res.stdout).strip().splitlines()
        hint = message[-1] if message else "git failed"
        if "not found" in hint.lower() or "could not read" in hint.lower() or "authentication" in hint.lower():
            hint += " (does the wiki exist? For a private repository add an access token with read access.)"
        raise DeliveryError(f"Couldn't fetch the wiki: {hint}"[:500], retry=False)
    return res.stdout.strip()


def fetch(config, token: str, dest: str) -> str:
    url = clone_url(config)
    ensure_public_url(url)
    branch = config.get("branch") or ""
    path = (config.get("path") or "").strip("/")
    args = ["clone", "--depth", "1", "--single-branch", "--no-tags"]
    if branch:
        args += ["--branch", branch]
    if path:
        args += ["--sparse", "--filter=blob:none"]  # only the folder's files are downloaded
    git([*args, url, dest], token=token)
    if path:
        git(["sparse-checkout", "set", "--no-cone", f"/{path}/"], cwd=dest, token=token)
    return git(["rev-parse", "HEAD"], cwd=dest)


def read_pages(config, root: Path) -> list[dict]:
    source = config.get("source", "github_wiki")
    base = root / (config.get("path") or "").strip("/")
    pages, total = [], 0
    for file in sorted(base.rglob("*")):
        rel = file.relative_to(root).as_posix()
        if not file.is_file() or file.suffix.lower() not in EXTENSIONS or "/." in f"/{rel}":
            continue
        if source == "github_wiki" and file.name.startswith("_"):
            continue  # _Sidebar, _Footer
        data = file.read_bytes()[:MAX_PAGE_BYTES]
        total += len(data)
        if total > MAX_TOTAL_BYTES or len(pages) >= MAX_PAGES:
            break
        text = data.decode("utf-8", errors="replace").strip()
        if text:
            pages.append({"path": rel[:500], "title": title_of(rel, text, source), "content": text, "url": page_url(config, rel)[:700]})
    return pages


def store(integration, state, pages):
    with transaction.atomic():
        keep = []
        for p in pages:
            page, _ = WikiPage.objects.update_or_create(integration=integration, path=p["path"], defaults=p)
            keep.append(page.pk)
        WikiPage.objects.filter(integration=integration).exclude(pk__in=keep).delete()
    state.pages = len(pages)


def sync(integration, force=False) -> WikiSource:
    """Refresh one wiki; raises DeliveryError (also recorded on the source) when it fails."""
    state, _ = WikiSource.objects.get_or_create(integration=integration)
    token = decrypt(integration.credentials.get("api_key", "")) if integration.credentials.get("api_key") else ""
    try:
        if integration.config.get("source") == "mediawiki":
            from . import mediawiki

            commit, pages = mediawiki.fetch(integration.config, token)
            if commit != state.commit or force or not state.pages:
                store(integration, state, pages)
        else:
            with tempfile.TemporaryDirectory(prefix="flowly-wiki-") as tmp:
                dest = os.path.join(tmp, "w")
                commit = fetch(integration.config, token, dest)
                if commit != state.commit or force or not state.pages:
                    store(integration, state, read_pages(integration.config, Path(dest)))
        state.commit = commit[:64]
    except DeliveryError as exc:
        state.error = str(exc)
        state.synced_at = timezone.now()
        state.save()
        raise
    state.error = ""
    state.synced_at = timezone.now()
    state.save()
    return state


_last_tick = 0.0


def tick(limit=3) -> int:
    """Worker: refresh wikis not synced in the last few hours (checked every few minutes)."""
    global _last_tick
    import time

    from django.db.models import Q

    if time.monotonic() - _last_tick < 300:
        return 0
    _last_tick = time.monotonic()

    from integrations.models import Integration

    due = (Integration.objects.filter(kind="wiki", enabled=True)
           .filter(Q(wiki__isnull=True) | Q(wiki__synced_at__isnull=True) | Q(wiki__synced_at__lt=timezone.now() - EVERY))[:limit])
    due = list(due)
    for integration in due:
        try:
            sync(integration)
        except DeliveryError as exc:
            log.info("Wiki %s: %s", integration.pk, exc)
        except Exception:
            log.exception("Wiki %s sync crashed", integration.pk)
    return len(due)
