"""OpenID Connect, authorization code flow with PKCE: discovery, the authorize URL, the code exchange and the ID token
check. Every call to the provider goes through integrations.http.send, so private addresses are refused."""

import base64
import hashlib
import json
import secrets
from urllib.parse import urlencode

import jwt

from integrations.crypto import decrypt
from integrations.http import DeliveryError, send

#: Asymmetric algorithms only: HS256 would let anyone who knows the client secret mint tokens.
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]


class OidcError(Exception):
    pass


def discover(issuer: str) -> dict:
    issuer = issuer.rstrip("/")
    try:
        meta = send("GET", f"{issuer}/.well-known/openid-configuration", timeout=10).json()
    except DeliveryError as exc:
        raise OidcError(f"Couldn't read the provider's configuration at {issuer}: {exc}") from exc
    except ValueError as exc:
        raise OidcError("The provider's configuration isn't valid JSON.") from exc
    for key in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not meta.get(key):
            raise OidcError(f"The provider's configuration has no {key}.")
    # Entra's common endpoint advertises "{tenantid}"; everyone else must name exactly the issuer we were given.
    if meta["issuer"].rstrip("/") != issuer and "{tenantid}" not in meta["issuer"]:
        raise OidcError(f"The provider says its issuer is {meta['issuer']}, not {issuer}.")
    return meta


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(meta: dict, client_id: str, redirect_uri: str, state: str, nonce: str, challenge: str, login_hint="") -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    sep = "&" if "?" in meta["authorization_endpoint"] else "?"
    return f"{meta['authorization_endpoint']}{sep}{urlencode(params)}"


def exchange(meta: dict, connection, code: str, verifier: str, redirect_uri: str) -> dict:
    secret = decrypt(connection.client_secret) if connection.client_secret else ""
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier,
            "client_id": connection.client_id}
    headers = {"Accept": "application/json"}
    methods = meta.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if secret and "client_secret_basic" in methods:
        raw = f"{connection.client_id}:{secret}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    elif secret:
        data["client_secret"] = secret
    try:
        return send("POST", meta["token_endpoint"], data=data, headers=headers, timeout=15).json()
    except DeliveryError as exc:
        detail = ""
        try:
            detail = json.loads(exc.body or "{}").get("error_description") or json.loads(exc.body or "{}").get("error") or ""
        except ValueError:
            pass
        raise OidcError(f"The provider refused the sign-in{': ' + detail if detail else ''}.") from exc


def verify(meta: dict, id_token: str, client_id: str, nonce: str) -> dict:
    """Signature (against the provider's published keys), issuer, audience, expiry and nonce."""
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise OidcError("The provider sent an unreadable ID token.") from exc
    if header.get("alg") not in ALGORITHMS:
        raise OidcError(f"Unsupported ID token algorithm {header.get('alg')}.")
    try:
        keys = jwt.PyJWKSet.from_dict(send("GET", meta["jwks_uri"], timeout=10).json())
    except (DeliveryError, ValueError, jwt.PyJWTError) as exc:
        raise OidcError("Couldn't load the provider's signing keys.") from exc
    candidates = [k for k in keys.keys if not header.get("kid") or k.key_id == header.get("kid")]
    if not candidates:
        raise OidcError("The ID token is signed with an unknown key.")
    unverified = jwt.decode(id_token, options={"verify_signature": False})
    issuer = meta["issuer"]
    if "{tenantid}" in issuer:
        issuer = issuer.replace("{tenantid}", str(unverified.get("tid", "")))
    last = None
    for key in candidates:
        try:
            claims = jwt.decode(id_token, key.key, algorithms=[header["alg"]], audience=client_id, issuer=issuer, leeway=60,
                                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
            break
        except jwt.PyJWTError as exc:
            last = exc
    else:
        raise OidcError(f"The ID token didn't check out ({last}).")
    if claims.get("nonce") != nonce:
        raise OidcError("The sign-in doesn't match the one we started (nonce).")
    return claims


def email_of(claims: dict) -> str:
    """The person's email: `email` (unless the provider says it's unverified), else an email-shaped username (Entra)."""
    if claims.get("email") and claims.get("email_verified") is not False:
        return claims["email"].strip().lower()
    for key in ("preferred_username", "upn"):
        value = str(claims.get(key) or "").strip().lower()
        if "@" in value and "." in value.split("@")[-1]:
            return value
    raise OidcError("The provider didn't share a verified email address. Add the email scope or claim in its app settings.")
