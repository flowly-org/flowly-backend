"""Symmetric encryption for stored secrets (API keys), keyed from SECRET_KEY."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

PREFIX = "enc:"


def _fernet():
    digest = hashlib.sha256(f"flowly-secrets:{settings.SECRET_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    return PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value or not value.startswith(PREFIX):
        return value or ""
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        return ""


def mask(secret: str) -> str:
    return f"…{secret[-4:]}" if len(secret) > 8 else "set"
