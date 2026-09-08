"""API token extraction, status validation, and tenant-scope enforcement."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header

from morphlake.admin_store import AdminStore, TokenIdentity
from morphlake.errors import MorphLakeError


def get_admin_store() -> AdminStore:
    raise RuntimeError("Admin store dependency is not configured")


def require_token(
    store: Annotated[AdminStore, Depends(get_admin_store)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_token: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> TokenIdentity:
    token = x_api_token or x_api_key
    if authorization:
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() != "bearer" or not credentials:
            raise MorphLakeError("token_invalid_scheme", "Authorization must use Bearer token", 401)
        token = credentials
    return store.authenticate(token)


def write_scope(identity: TokenIdentity) -> tuple[str, str]:
    """Uploads always inherit the key's configured domain and department."""
    return identity.business_domain, identity.department


def read_scope(identity: TokenIdentity) -> tuple[str | None, str | None]:
    """Administration keys read all data; domain keys read their entire domain."""
    if identity.access_level == "admin":
        return None, None
    return identity.business_domain, None


def require_admin_token(
    identity: Annotated[TokenIdentity, Depends(require_token)],
) -> TokenIdentity:
    """Restrict administration APIs to the seeded all-domain administration key."""
    if identity.access_level != "admin":
        raise MorphLakeError(
            "admin_key_required",
            "This endpoint requires an administration key",
            403,
        )
    return identity


def enforce_asset_access(identity: TokenIdentity, business_domain: str) -> None:
    if identity.access_level != "admin" and business_domain != identity.business_domain:
        raise MorphLakeError(
            "token_scope_mismatch", "Key cannot access data outside its business domain", 403
        )
