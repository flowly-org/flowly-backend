"""Read a MediaWiki (Wikipedia's software) through its API: every page of a namespace, or of one category.

Private wikis: a bot password (Special:BotPasswords) with read rights; the username looks like "Name@flowly".
"""

import hashlib
import re
from urllib.parse import quote

import requests

from integrations.http import USER_AGENT, DeliveryError, ensure_public_url

MAX_PAGES = 500
MAX_PAGE_CHARS = 200_000
MAX_TOTAL_CHARS = 5_000_000


class Client:
    def __init__(self, url: str, username="", password=""):
        self.base = url.strip().rstrip("/")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = f"{USER_AGENT} (https://flowly.net; wiki sync)"
        self.username, self.password = username, password
        self.api = ""

    def _get(self, api, **params):
        ensure_public_url(api)
        try:
            res = self.session.get(api, params={"format": "json", "formatversion": "2", **params}, timeout=30)
        except requests.RequestException as exc:
            raise DeliveryError(f"Couldn't reach the wiki: {exc}") from exc
        if res.status_code >= 400:
            raise DeliveryError(f"The wiki answered HTTP {res.status_code}.", status=res.status_code, retry=res.status_code >= 500)
        try:
            data = res.json()
        except ValueError:
            return None
        if isinstance(data, dict) and data.get("error"):
            raise DeliveryError(f"MediaWiki: {data['error'].get('info') or data['error'].get('code')}", retry=False)
        return data

    def discover(self) -> dict:
        """Find api.php from the address given (the API itself, the wiki's root, or a page) and return siteinfo."""
        if self.base.endswith("api.php"):
            candidates = [self.base]
        else:
            root = re.sub(r"/(wiki|index\.php)(/.*)?$", "", self.base)
            candidates = [f"{root}/api.php", f"{root}/w/api.php"]
        for api in candidates:
            try:
                data = self._get(api, action="query", meta="siteinfo", siprop="general")
            except DeliveryError as exc:
                if exc.status in (404, 403) or exc.status is None:
                    continue
                raise
            if isinstance(data, dict) and data.get("query", {}).get("general"):
                self.api = api
                return data["query"]["general"]
        raise DeliveryError("No MediaWiki API found at that address (tried api.php and w/api.php).", retry=False)

    def login(self):
        if not self.username:
            return
        token = self._get(self.api, action="query", meta="tokens", type="login")["query"]["tokens"]["logintoken"]
        try:
            res = self.session.post(self.api, data={"action": "login", "lgname": self.username, "lgpassword": self.password,
                                                    "lgtoken": token, "format": "json", "formatversion": "2"}, timeout=30)
            result = res.json().get("login", {})
        except (requests.RequestException, ValueError) as exc:
            raise DeliveryError(f"Couldn't sign in to the wiki: {exc}") from exc
        if result.get("result") != "Success":
            raise DeliveryError(f"The wiki refused the bot password: {result.get('reason') or result.get('result')}", retry=False)

    def pages(self, *, namespace=0, category=""):
        """Yield (pageid, title, revid, wikitext) for every page, 50 per request."""
        if category:
            title = category if ":" in category else f"Category:{category}"
            params = {"generator": "categorymembers", "gcmtitle": title, "gcmlimit": "50", "gcmtype": "page"}
        else:
            params = {"generator": "allpages", "gapnamespace": str(namespace), "gaplimit": "50", "gapfilterredir": "nonredirects"}
        cont = {}
        while True:
            data = self._get(self.api, action="query", prop="revisions", rvprop="ids|content", rvslots="main", **params, **cont)
            for page in (data or {}).get("query", {}).get("pages", []):
                rev = (page.get("revisions") or [{}])[0]
                text = (rev.get("slots", {}).get("main", {}) or {}).get("content", "") or rev.get("content", "")
                yield page.get("pageid"), page.get("title", ""), rev.get("revid", 0), text
            if not (data or {}).get("continue"):
                break
            cont = data["continue"]


def to_text(wikitext: str) -> str:
    """Readable text from wikitext: links and formatting simplified (templates and refs dropped)."""
    t = re.sub(r"<ref[^>/]*/>|<ref[^>]*>.*?</ref>", "", wikitext or "", flags=re.S | re.I)
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    for _ in range(3):  # nested templates
        t = re.sub(r"\{\{[^{}]*\}\}", "", t)
    t = re.sub(r"\[\[(?:File|Image|Category|Datei|File|Immagine|Categoria|Fichier|Catégorie|Kategorie|Categoría|Imagen):[^\]]*\]\]", "", t, flags=re.I)
    t = re.sub(r"\[\[([^|\]]*)\|([^\]]*)\]\]", r"\2", t)
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[(https?://\S+) ([^\]]+)\]", r"\2 (\1)", t)
    t = re.sub(r"'{2,5}", "", t)
    t = re.sub(r"^(=+)\s*(.*?)\s*\1\s*$", lambda m: "#" * min(len(m.group(1)), 6) + " " + m.group(2), t, flags=re.M)
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def fetch(config: dict, password: str) -> tuple[str, list[dict]]:
    """(version, pages) of the wiki; the version changes whenever any page does."""
    client = Client(config["url"], config.get("username", ""), password)
    site = client.discover()
    client.login()
    server, path = site.get("server", ""), site.get("articlepath", "/wiki/$1")
    if server.startswith("//"):
        server = "https:" + server
    pages, total, ids = [], 0, []
    for pageid, title, revid, text in client.pages(namespace=int(config.get("namespace") or 0), category=config.get("category", "")):
        ids.append(f"{pageid}:{revid}")
        body = to_text(text)[:MAX_PAGE_CHARS]
        total += len(body)
        if len(pages) >= MAX_PAGES or total > MAX_TOTAL_CHARS:
            break
        if body:
            url = server + path.replace("$1", quote(title.replace(" ", "_"), safe="/:()'"))
            pages.append({"path": f"{pageid}"[:500], "title": title[:300], "content": body, "url": url[:700]})
    version = hashlib.sha1(",".join(sorted(ids)).encode()).hexdigest()
    return version, pages
