import re

from rest_framework import serializers

from ..crypto import decrypt, mask
from .base import Connector

REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
SAFE_RE = re.compile(r"^[\w./ -]*$")
SOURCES = ("github_wiki", "github_repo", "git")


class Wiki(Connector):
    """An external wiki (for now a GitHub wiki, a docs folder of a repository, or any https git repository), synced
    as text so Flo and the delegation agent can use it as context. Opt-in, per workspace or project."""

    kind = "wiki"
    has_secret = True

    def clean_config(self, config, integration):
        source = config.get("source") or "github_wiki"
        if source not in SOURCES:
            raise serializers.ValidationError({"source": f"Use one of: {', '.join(SOURCES)}."})
        out = {"source": source}
        if source == "git":
            url = (config.get("url") or "").strip()
            if not url.startswith("https://") or " " in url:
                raise serializers.ValidationError({"url": "Enter the repository's https clone URL."})
            out["url"] = url[:400]
        else:
            repo = (config.get("repo") or "").strip().removeprefix("https://github.com/").removesuffix(".git").removesuffix("/wiki").strip("/")
            if not REPO_RE.match(repo):
                raise serializers.ValidationError({"repo": 'Use the form "owner/repository".'})
            out["repo"] = repo
        for key in ("path", "branch"):
            value = (config.get(key) or "").strip().strip("/")
            if value and (not SAFE_RE.match(value) or ".." in value or value.startswith("-")):
                raise serializers.ValidationError({key: "Letters, digits, '/', '.', '-' and '_' only."})
            if value and source != "github_wiki":
                out[key] = value[:200]
        return out

    def info(self, integration):
        from wiki.models import WikiSource
        from wiki.sync import github_web

        state = WikiSource.objects.filter(integration=integration).first()
        cfg = integration.config
        if cfg.get("source") == "github_wiki":
            web = f"{github_web()}/{cfg.get('repo')}/wiki"
        elif cfg.get("source") == "github_repo":
            web = f"{github_web()}/{cfg.get('repo')}" + (f"/tree/{cfg.get('branch') or 'HEAD'}/{cfg['path']}" if cfg.get("path") else "")
        else:
            web = cfg.get("url", "")
        key = decrypt(integration.credentials.get("api_key", "")) if integration.credentials.get("api_key") else ""
        return {
            "web_url": web, "pages": state.pages if state else 0, "synced_at": state.synced_at if state else None,
            "error": state.error if state else "", "api_key": mask(key) if key else None,
        }

    def test(self, integration) -> str:
        from wiki.sync import sync

        state = sync(integration, force=True)
        return f"Synced {state.pages} page(s) (commit {state.commit[:7]})."
