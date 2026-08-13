"""Who is calling.

S06 replaced the believed-header stub with real verification of Google and Apple ID tokens.
A caller sends `Authorization: Bearer <token>` from Google or Apple Sign-In; this module checks
the signature against the provider's published keys, checks the audience and issuer, and only
then trusts the `sub` claim as the identity. The `X-Story-Identity` dev header still exists for
local development, gated by `Settings.allow_dev_identity_header` -- **flip that to `False` before
any deployment is reachable beyond localhost** (D14's condition for exposing this service).

What must never happen: a bearer token that fails verification falling back to the dev header.
That would turn "send a bad token" into a way to impersonate anyone via the header, silently
downgrading a hardened deployment back to the stub. So an `Authorization` header, once present,
either produces a verified `Principal` or a 401 -- it never falls through.

The shape this preserves: every route already takes an `owner_id` and every index read is scoped
by it in SQL, so switching from a believed header to a verified token does not touch a query.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import Header, HTTPException, Request

from storybook_service.index import Index
from storybook_service.settings import Settings

DEV_IDENTITY_HEADER = "X-Story-Identity"

# Real Google ID tokens carry `iss` as either form historically; both are accepted. Apple has
# used only the one. An issuer outside this list is refused outright -- it is never routed to a
# JWKS fetch, let alone trusted.
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
APPLE_ISSUERS = ("https://appleid.apple.com",)

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    identity_kind: str
    identity_value: str
    authenticated: bool
    """Reported rather than assumed -- an artifact never overstates its contents."""


def identity_kind_for(value: str) -> str:
    """Only used by the dev-header path, which has no provider to infer a kind from."""
    return "email" if "@" in value else "phone"


def _provider_for_issuer(issuer: str) -> tuple[str, str, str] | None:
    """Return (kind, jwks_url, audience_setting_name) for a known issuer, else None."""
    if issuer in GOOGLE_ISSUERS:
        return ("google", GOOGLE_JWKS_URL, "google_client_id")
    if issuer in APPLE_ISSUERS:
        return ("apple", APPLE_JWKS_URL, "apple_client_id")
    return None


# One client per JWKS URL, reused across requests -- PyJWKClient caches the fetched key set
# internally, so building a new one per request would refetch every time.
_jwk_clients: dict[str, jwt.PyJWKClient] = {}


def _jwk_client(jwks_url: str) -> jwt.PyJWKClient:
    client = _jwk_clients.get(jwks_url)
    if client is None:
        client = jwt.PyJWKClient(jwks_url)
        _jwk_clients[jwks_url] = client
    return client


def _verify_bearer_token(token: str, settings: Settings) -> tuple[str, str]:
    """Return (kind, sub) for a verified token, or raise HTTPException(401)."""
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"unparseable bearer token: {exc}") from None

    issuer = unverified.get("iss", "")
    provider = _provider_for_issuer(issuer)
    if provider is None:
        raise HTTPException(status_code=401, detail=f"unrecognised token issuer: {issuer!r}")
    kind, jwks_url, audience_field = provider

    audience = getattr(settings, audience_field)
    if not audience:
        raise HTTPException(
            status_code=401,
            detail=f"{kind} sign-in is not configured on this deployment",
        )

    try:
        signing_key = _jwk_client(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"invalid {kind} token: {exc}") from None

    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail=f"{kind} token has no 'sub' claim")
    return kind, sub


def resolve_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    x_story_identity: str | None = Header(default=None, alias=DEV_IDENTITY_HEADER),
) -> Principal:
    settings: Settings = request.app.state.settings
    index: Index = request.app.state.index

    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(
                status_code=401, detail="Authorization header must be 'Bearer <token>'"
            )
        kind, sub = _verify_bearer_token(token, settings)
        user = index.ensure_user(kind=kind, value=sub)
        return Principal(
            user_id=user.id,
            identity_kind=kind,
            identity_value=sub,
            authenticated=True,
        )

    if x_story_identity and settings.allow_dev_identity_header:
        kind = identity_kind_for(x_story_identity)
        user = index.ensure_user(kind=kind, value=x_story_identity)
        return Principal(
            user_id=user.id,
            identity_kind=kind,
            identity_value=x_story_identity,
            authenticated=False,
        )

    if not settings.allow_dev_identity_header:
        raise HTTPException(status_code=401, detail="no caller identity. Sign in.")

    raise HTTPException(
        status_code=401,
        detail=(
            f"no caller identity. Send {DEV_IDENTITY_HEADER}: <email or phone>, or "
            "Authorization: Bearer <Google or Apple ID token>."
        ),
    )
