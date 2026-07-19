"""Strict canonical JSON and ``sha256:`` content hashes for receipts.

This module owns the one canonical byte encoding behind every receipt
content hash. The rules are frozen by the Verified Outcome & Artifact
Receipts plan and shared by every consumer:

- UTF-8 JSON with sorted string keys and compact separators.
- NFC-normalized strings; mapping keys must be strings.
- UTC RFC 3339 timestamps (``...Z``); naive datetimes are rejected.
- Booleans, ``None``, integers, and finite ``float``/``Decimal`` values
  only; NaN and infinities are rejected loudly.
- Tuples and lists both render as JSON arrays; frozen dataclasses render
  as their field mapping.
- Bytes, paths, sets, and unknown objects are rejected — canonical
  hashing never guesses at a representation.

Interoperability invariant: the canonical bytes of ``{"answer": 42}`` are
exactly ``{"answer":42}`` and hash to
``sha256:ecf59a2696ca44a417e20e2a7eabb1b26e82c779f8546bea354a2cc80e8e1eed``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import PurePath
from typing import Mapping

__all__ = [
    "CANONICAL_HASH_PREFIX",
    "canonical_content_hash",
    "hash_hex",
    "normalize_utc_timestamp",
]

CANONICAL_HASH_PREFIX = "sha256:"


def canonical_content_hash(value: object) -> str:
    """Hash *value* as strict canonical JSON, returning ``sha256:<64 hex>``."""
    normalized = _normalize(value)
    payload = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def hash_hex(content_hash: str) -> str:
    """Return the bare 64-hex digest of a ``sha256:`` content hash."""
    if not content_hash.startswith(CANONICAL_HASH_PREFIX):
        raise ValueError(
            f"expected a '{CANONICAL_HASH_PREFIX}' content hash, got {content_hash!r}"
        )
    digest = content_hash[len(CANONICAL_HASH_PREFIX):]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError(f"malformed sha256 content hash: {content_hash!r}")
    return digest


def normalize_utc_timestamp(value: str | datetime) -> str:
    """Normalize a timezone-aware timestamp to canonical UTC RFC 3339.

    Accepts an RFC 3339 / ISO-8601 string or an aware ``datetime`` and
    returns ``YYYY-MM-DDTHH:MM:SS[.ffffff]Z``. Naive datetimes are
    rejected: a receipt timestamp without a zone is not a fact.
    """
    if isinstance(value, str):
        text = unicodedata.normalize("NFC", value)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid RFC 3339 timestamp: {value!r}") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError(
            f"timestamp must be str or datetime, got {type(value).__name__}"
        )
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(
            "naive datetime rejected: receipt timestamps must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize(value: object) -> object:
    """Recursively convert *value* into strict JSON-canonical structures."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float rejected: hash inputs must be finite")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal rejected: hash inputs must be finite")
        integral = value.to_integral_value()
        if value == integral:
            return int(integral)
        return float(value)
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, datetime):
        # normalize_utc_timestamp rejects naive datetimes.
        return normalize_utc_timestamp(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError("bytes rejected: hash canonical JSON values, not raw bytes")
    if isinstance(value, PurePath):
        raise TypeError(
            "path objects rejected: canonical content never embeds local paths"
        )
    if isinstance(value, (set, frozenset)):
        raise TypeError("sets rejected: canonical JSON has no unordered collections")
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"non-string mapping key rejected: {key!r} "
                    f"({type(key).__name__})"
                )
            canonical_key = unicodedata.normalize("NFC", key)
            if canonical_key in normalized:
                raise ValueError(
                    f"duplicate mapping key after NFC normalization: {key!r}"
                )
            normalized[canonical_key] = _normalize(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _normalize(
            {
                field.name: getattr(value, field.name)
                for field in dataclasses.fields(value)
            }
        )
    raise TypeError(
        f"cannot canonicalize object of type {type(value).__name__}; "
        "receipt hash inputs are strict JSON values"
    )
