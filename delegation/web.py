"""Reading the web and attachments for the agent: text only, size-capped, private addresses refused."""

import io
import re
from html.parser import HTMLParser

import requests

from integrations.http import DeliveryError, ensure_public_url

MAX_BYTES = 3 * 1024 * 1024
MAX_CHARS = 15000


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}

    def __init__(self):
        super().__init__()
        self.parts, self.title, self._skip, self._in_title = [], "", 0, False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "tr", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_text(html: str) -> tuple[str, str]:
    parser = _Text()
    parser.feed(html)
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return parser.title.strip(), text


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages[:40]).strip()


def to_text(data: bytes, content_type: str, name: str = "") -> tuple[str, str]:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct == "application/pdf" or name.lower().endswith(".pdf"):
        return name, pdf_text(data)
    if ct in ("text/html", "application/xhtml+xml") or name.lower().endswith((".html", ".htm")):
        return html_text(data.decode("utf-8", "replace"))
    if ct.startswith("text/") or ct in ("application/json", "application/xml") or name.lower().endswith((".md", ".txt", ".csv", ".json")):
        return name, data.decode("utf-8", "replace")
    raise ValueError(f"Can't read {ct or 'this file type'} as text.")


def fetch(url: str) -> str:
    ensure_public_url(url)
    try:
        res = requests.get(url, timeout=15, stream=True, headers={"User-Agent": "Mozilla/5.0 (compatible; FlowlyAgent/1.0)"},
                           allow_redirects=True)
        if res.url != url:
            ensure_public_url(res.url)
        data = res.raw.read(MAX_BYTES + 1, decode_content=True)
    except requests.RequestException as exc:
        raise DeliveryError(f"Couldn't open the page: {exc}") from exc
    if res.status_code >= 400:
        raise DeliveryError(f"The page answered HTTP {res.status_code}.")
    title, text = to_text(data[:MAX_BYTES], res.headers.get("Content-Type", ""), url)
    out = (f"# {title}\n\n" if title else "") + text
    return out[:MAX_CHARS] + ("\n\n[…truncated]" if len(out) > MAX_CHARS else "")
