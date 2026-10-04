"""Find wiki pages for a question or a task, and turn them into context for Flo and the delegation agent."""

import re
import unicodedata

from django.db.models import Q

from .models import WikiPage

WORD = re.compile(r"\w{3,}", re.U)
STOP = {"the", "and", "for", "with", "from", "this", "that", "una", "per", "con", "che", "del", "della", "dei", "gli",
        "les", "des", "pour", "und", "der", "die", "das", "los", "las", "por", "para", "are", "was", "have", "into"}
HOME_NAMES = {"home", "readme", "index", "start", "overview"}


def norm(text: str) -> str:
    return unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()


def terms(text: str) -> list[str]:
    return list(dict.fromkeys(w for w in WORD.findall(norm(text)) if w not in STOP))[:20]


def sources(workspace_ids, project_id=None, *, any_project=True):
    """Enabled wiki integrations of these workspaces: workspace-wide ones, plus the project's (or every project's)."""
    from integrations.models import Integration

    qs = Integration.objects.filter(kind="wiki", enabled=True, workspace_id__in=list(workspace_ids))
    if not any_project:
        qs = qs.filter(Q(project__isnull=True) | Q(project_id=project_id))
    return qs.select_related("workspace", "project")


def pages_of(integrations):
    return WikiPage.objects.filter(integration__in=list(integrations)).select_related("integration__workspace", "integration__project")


def score(page, words, boost=None) -> float:
    title, body = norm(page.title + " " + page.path), norm(page.content)
    s = 0.0
    for w in words:
        if w in title:
            s += 6
        s += min(body.count(w), 6)
    if s and boost and page.integration.project_id == boost:
        s *= 1.5
    return s


def search(integrations, query: str, limit=5, boost_project=None):
    words = terms(query)
    if not words:
        return []
    scored = [(score(p, words, boost_project), p) for p in pages_of(integrations)]
    return [p for s, p in sorted(scored, key=lambda x: -x[0]) if s > 0][:limit]


def snippet(page, query: str, size=600) -> str:
    text, low = page.content, norm(page.content)
    hits = [low.find(w) for w in terms(query) if low.find(w) >= 0]
    start = max(0, min(hits) - size // 4) if hits else 0
    out = text[start:start + size].strip()
    return ("…" if start else "") + out + ("…" if start + size < len(text) else "")


def where(page) -> str:
    integ = page.integration
    return integ.workspace.name + (f" / {integ.project.name}" if integ.project_id else "")


def describe_hits(pages, query: str) -> str:
    return "\n\n".join(f"[W{p.id}] {p.title} ({where(p)}{', ' + p.url if p.url else ''})\n{snippet(p, query)}" for p in pages)


def read(integrations, page_id, limit=15000) -> str:
    page = pages_of(integrations).filter(pk=int(str(page_id).lstrip("Ww"))).first()
    if page is None:
        return "No such wiki page."
    more = f"\n\n[… {len(page.content) - limit} more characters]" if len(page.content) > limit else ""
    return f"{page.title} ({where(page)}{', ' + page.url if page.url else ''})\n\n{page.content[:limit]}{more}"


def task_sources(task):
    return sources([task.workspace_id], task.project_id, any_project=False)


def task_context(task, budget=6000) -> str:
    """Wiki knowledge for a task: the index, the overview page(s) and the pages that match it best."""
    integrations = list(task_sources(task))
    if not integrations:
        return ""
    pages = list(pages_of(integrations))
    if not pages:
        return ""
    query = " ".join(filter(None, [task.title, task.description[:400], task.project.name if task.project_id else ""]))
    lines = ["Team wiki (the workspace's own knowledge: follow its conventions and facts over general assumptions; "
             "cite the page titles you rely on; read more pages with search_wiki / read_wiki_page).",
             "Pages: " + "; ".join(f"W{p.id} {p.title}" for p in pages[:80]) + (" …" if len(pages) > 80 else "")]
    used = sum(len(x) for x in lines)
    homes = [p for p in pages if norm(p.title).replace(" ", "") in HOME_NAMES or norm(p.path.rsplit("/", 1)[-1].rsplit(".", 1)[0]) in HOME_NAMES]
    # The project's overview first.
    homes.sort(key=lambda p: p.integration.project_id != task.project_id)
    chosen = homes[:2] + [p for p in search(integrations, query, limit=6, boost_project=task.project_id) if p not in homes[:2]]
    for p in chosen:
        room = budget - used
        if room < 400:
            break
        body = p.content[:min(2500, room - 100)] if p in homes else snippet(p, query, size=min(1500, room - 100))
        block = f"\n## W{p.id} {p.title} ({where(p)})\n{body}"
        lines.append(block)
        used += len(block)
    return "\n".join(lines)
