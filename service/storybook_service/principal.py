"""Who is calling -- and an honest statement that today nobody checks.

**There is no authentication in this service yet.** S06 owns Google (first) and Apple (later) sign
-in; until it lands, the caller states its own identity in a header and is believed. That is fine
for local development and is a hole in production, so it is written here in one place rather than
spread across routes, and it fails closed when the header is absent instead of inventing a default
user -- a default user is how a deployment ends up with every trip in one account.

What S06 replaces is this function's body. What it must not have to replace is the shape: every
route already takes an `owner_id` and every index read is scoped by it in SQL, so switching from a
believed header to a verified token does not touch a query.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException, Request

from storybook_service.index import Index

# The identity kinds question 17 names are `email` and `phone`. The header carries whichever, and
# the kind is inferred only so the index can enforce uniqueness per kind.
DEV_IDENTITY_HEADER = "X-Story-Identity"


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    identity_kind: str
    identity_value: str
    authenticated: bool
    """False for every caller today, and reported rather than assumed.

    An artifact never overstates its contents, and "who this is" is the artifact a route consumes.
    """


def identity_kind_for(value: str) -> str:
    return "email" if "@" in value else "phone"


def resolve_principal(
    request: Request,
    x_story_identity: str | None = Header(default=None, alias=DEV_IDENTITY_HEADER),
) -> Principal:
    if not x_story_identity:
        raise HTTPException(
            status_code=401,
            detail=(
                f"no caller identity. Send {DEV_IDENTITY_HEADER}: <email or phone>. "
                "This is an unauthenticated development stand-in; S06 replaces it with verified "
                "Google and Apple sign-in."
            ),
        )
    index: Index = request.app.state.index
    kind = identity_kind_for(x_story_identity)
    user = index.ensure_user(kind=kind, value=x_story_identity)
    return Principal(
        user_id=user.id,
        identity_kind=kind,
        identity_value=x_story_identity,
        authenticated=False,
    )
