"""Stable partition helpers for high-volume Paimon tables."""

from __future__ import annotations

import hashlib


def domain_shard(business_domain: str, shard_count: int) -> int:
    """Map a business domain to a stable physical shard."""
    if shard_count <= 0:
        raise ValueError("shard_count must be positive")
    digest = hashlib.sha256(business_domain.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % shard_count
