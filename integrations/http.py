import ipaddress
import socket
from urllib.parse import urlparse

import requests

from instance import conf

USER_AGENT = "Flowly-Integrations/1.0"


class DeliveryError(Exception):
    def __init__(self, message, status=None, body="", retry=True):
        super().__init__(message)
        self.status = status
        self.body = body
        self.retry = retry


def ensure_public_url(url):
    """Refuse URLs pointing at private/loopback/link-local hosts (SSRF guard for user-supplied URLs)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise DeliveryError(f"Invalid URL: {url}", retry=False)
    if conf.get("INTEGRATIONS_ALLOW_PRIVATE_URLS"):
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise DeliveryError(f"Cannot resolve {parsed.hostname}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise DeliveryError(f"{parsed.hostname} resolves to a private address ({ip}); not allowed.", retry=False)


def send(method, url, *, json=None, data=None, headers=None, timeout=10, check_url=True):
    """HTTP call that turns non-2xx answers and network errors into DeliveryError."""
    if check_url:
        ensure_public_url(url)
    all_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    try:
        res = requests.request(method, url, json=json, data=data, headers=all_headers, timeout=timeout)
    except requests.RequestException as exc:
        raise DeliveryError(f"Request failed: {exc}") from exc
    if res.status_code >= 400:
        # 4xx (except throttling) won't fix itself by retrying.
        retry = res.status_code >= 500 or res.status_code in (408, 409, 429)
        raise DeliveryError(f"HTTP {res.status_code}", status=res.status_code, body=res.text[:2000], retry=retry)
    return res
