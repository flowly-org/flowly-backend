"""A small CalDAV client (RFC 4791) on requests: discover the calendars, query events in a time range.

Every address is checked against private networks (the server URL is user-supplied).
"""

from datetime import timezone as dt_timezone
from urllib.parse import urljoin

import requests
from lxml import etree

from integrations.crypto import decrypt
from integrations.http import USER_AGENT, DeliveryError, ensure_public_url

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "a": "http://apple.com/ns/ical/"}
PROP_PRINCIPAL = '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>'
PROP_HOME = '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><c:calendar-home-set/></d:prop></d:propfind>'
PROP_CALS = ('<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:a="http://apple.com/ns/ical/">'
             '<d:prop><d:displayname/><d:resourcetype/><a:calendar-color/><c:supported-calendar-component-set/></d:prop></d:propfind>')
QUERY = ('<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><d:getetag/><c:calendar-data/></d:prop>'
         '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"><c:time-range start="{lo}" end="{hi}"/>'
         '</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>')


class Client:
    def __init__(self, url: str, username: str, password: str):
        self.base = url.strip()
        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.headers["User-Agent"] = USER_AGENT

    @classmethod
    def for_account(cls, account):
        return cls(account.server_url, account.username, decrypt(account.refresh_token) if account.refresh_token else "")

    def request(self, method, url, body="", depth="0"):
        ensure_public_url(url)
        try:
            res = self.session.request(method, url, data=body.encode(), timeout=30, allow_redirects=False,
                                       headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"})
        except requests.RequestException as exc:
            raise DeliveryError(f"Couldn't reach the CalDAV server: {exc}") from exc
        if res.status_code in (301, 302, 307, 308) and res.headers.get("Location"):
            return self.request(method, urljoin(url, res.headers["Location"]), body, depth)
        if res.status_code == 401:
            raise DeliveryError("The CalDAV server refused the username or password (use an app password if you have 2FA).", status=401, retry=False)
        if res.status_code >= 400:
            raise DeliveryError(f"CalDAV: HTTP {res.status_code} for {method} {url}", status=res.status_code, retry=res.status_code >= 500)
        try:
            return etree.fromstring(res.content)
        except etree.XMLSyntaxError as exc:
            raise DeliveryError("The CalDAV server didn't answer with XML: is the address right?", retry=False) from exc

    def _href(self, tree, xpath, base):
        found = tree.xpath(xpath, namespaces=NS)
        return urljoin(base, found[0].strip()) if found else None

    def discover(self) -> list[dict]:
        """Calendars with events: [{"id": url, "name", "color"}]."""
        start = self.base
        candidates = [start, urljoin(start + "/", "/.well-known/caldav")]
        principal = None
        for url in candidates:
            try:
                tree = self.request("PROPFIND", url, PROP_PRINCIPAL)
            except DeliveryError as exc:
                if exc.status == 401:
                    raise
                continue
            principal = self._href(tree, "//d:current-user-principal/d:href/text()", url)
            if principal:
                break
        homes = []
        if principal:
            tree = self.request("PROPFIND", principal, PROP_HOME)
            home = self._href(tree, "//c:calendar-home-set/d:href/text()", principal)
            if home:
                homes.append(home)
        homes.append(start)  # the address may itself be a calendar (or a home)
        seen, out = set(), []
        for home in homes:
            try:
                tree = self.request("PROPFIND", home, PROP_CALS, depth="1")
            except DeliveryError as exc:
                if exc.status == 401:
                    raise
                continue
            for resp in tree.xpath("//d:response", namespaces=NS):
                if not resp.xpath(".//d:resourcetype/c:calendar", namespaces=NS):
                    continue
                comps = resp.xpath(".//c:supported-calendar-component-set/c:comp/@name", namespaces=NS)
                if comps and "VEVENT" not in comps:
                    continue  # task lists (VTODO) only
                href = urljoin(home, resp.xpath("d:href/text()", namespaces=NS)[0].strip())
                if href in seen:
                    continue
                seen.add(href)
                name = "".join(resp.xpath(".//d:displayname/text()", namespaces=NS)).strip() or href.rstrip("/").rsplit("/", 1)[-1]
                color = "".join(resp.xpath(".//a:calendar-color/text()", namespaces=NS)).strip()[:7]
                out.append({"id": href, "name": name[:255], "color": color})
            if out:
                break
        if not out:
            raise DeliveryError("No calendars found at that address. Try the server's CalDAV URL (e.g. https://cloud.example.org/remote.php/dav).", retry=False)
        return out

    def events(self, calendar_url: str, lo, hi) -> list[str]:
        """The iCalendar texts of the events overlapping [lo, hi)."""
        fmt = "%Y%m%dT%H%M%SZ"
        body = QUERY.format(lo=lo.astimezone(dt_timezone.utc).strftime(fmt), hi=hi.astimezone(dt_timezone.utc).strftime(fmt))
        tree = self.request("REPORT", calendar_url, body, depth="1")
        return [t for t in tree.xpath("//c:calendar-data/text()", namespaces=NS) if t.strip()]
