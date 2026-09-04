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


def enforce_scope(
    identity: TokenIdentity,
    business_domain: str | None,
    department: str | None,
) -> tuple[str, str]:
    if business_domain and business_domain != identity.business_domain:
        raise MorphLakeError(
            "token_scope_mismatch", "Token cannot access the requested business domain", 403
        )
    if department and department != identity.department:
        raise MorphLakeError(
            "token_scope_mismatch", "Token cannot access the requested department", 403
        )
    return identity.business_domain, identity.department
