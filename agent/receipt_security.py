"""Redaction, export, retention, and service-gated signing for receipts.

Owns the security surface of the canonical receipt contract:

- :class:`ReceiptRedactor` removes credential-like keys, bearer tokens,
  URL userinfo/query secrets, message bodies not explicitly declared
  evidence, and home/profile/sensitive path prefixes. Producers apply it
  BEFORE canonical receipt content is hashed or persisted; export applies
  it again as defense in depth.
- :class:`ReceiptExporter` writes public/local JSON exports and safe
  artifact bundles. Public export carries canonical receipt,
  observations, and attestations plus hash-verification data and never a
  raw local locator; local export may include profile-relative locators
  after boundary checks. Bundle entry names derive from ``artifact_id``
  plus a sanitized extension — display names are never trusted as paths
  — and artifact bytes are re-hashed while copying.
- :class:`ReceiptRetentionService` plans and prunes expired rows
  explicitly. ``plan()`` returns exact IDs and blockers; ``prune()``
  revalidates the plan, refuses active mission/transaction/legal/user
  holds, deletes expired raw artifact locators before receipt rows, and
  appends immutable deletion tombstones. It never runs implicitly during
  a live turn and never deletes artifact bytes outside the configured
  receipt artifact directory.
- :class:`ReceiptSigningService` and :func:`register_receipt_signer`
  gate optional signing providers. No provider loads until config names
  it and its ``check_fn`` accepts the config. A signature proves
  provenance over a content hash — it never changes a status, claim
  verdict, uncertainty, freshness, or scorer result.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol, runtime_checkable

from agent.receipt_artifacts import _redaction_prefixes
from agent.receipt_hashing import (
    canonical_content_hash,
    hash_hex,
    normalize_utc_timestamp,
)
from agent.receipt_models import (
    ArtifactDigest,
    EvidenceDigest,
    Receipt,
    ReceiptClaim,
    ReceiptObservation,
    ReceiptSourceKey,
    RequestedOutcome,
    build_observation,
)
from agent.receipt_store import (
    ReceiptAttestation,
    ReceiptStore,
    _rebuild_artifact,
    _rebuild_claim,
    _rebuild_evidence,
    _validate_receipt,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ReceiptExportError",
    "ReceiptExporter",
    "ReceiptRedactor",
    "ReceiptRetentionService",
    "ReceiptSecurityError",
    "ReceiptSigner",
    "ReceiptSigningService",
    "RetentionHold",
    "RetentionHoldError",
    "RetentionPlan",
    "RetentionPlanMismatch",
    "RetentionPruneResult",
    "SignatureMaterial",
    "SignatureVerification",
    "SignerFactory",
    "SigningUnavailableError",
    "register_receipt_signer",
    "unregister_receipt_signer",
    "verify_export_hashes",
]


class ReceiptSecurityError(RuntimeError):
    """Base error for receipt security operations."""


class ReceiptExportError(ReceiptSecurityError):
    """Export or bundle construction failed and nothing was shipped."""


class SigningUnavailableError(ReceiptSecurityError):
    """A required signing provider is unavailable or failed to sign."""


class RetentionError(ReceiptSecurityError):
    """Base error for explicit retention operations."""


class RetentionPlanMismatch(RetentionError):
    """The confirmed plan hash does not match the current plan."""


class RetentionHoldError(RetentionError):
    """An active mission/transaction/legal/user hold refuses the prune."""


def _now() -> str:
    return normalize_utc_timestamp(datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_REDACTED = "[REDACTED]"

# Substring markers for credential-like keys (case-insensitive).
_CREDENTIAL_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
    "access_key",
    "session_key",
    "signing_key",
    "client_id",
    "cookie",
    "bearer",
)

# Message-body-like keys are redacted unless explicitly declared evidence.
_MESSAGE_BODY_KEYS = frozenset(
    {"body", "message_body", "text_body", "html_body"}
)

_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_URL_USERINFO_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s@:]+):([^/\s@]+)@"
)
_URL_QUERY_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s\"'?]+)\?[^\s\"']*")


def _root_variants(roots: Iterable[object]) -> tuple[str, ...]:
    variants: set[str] = set()
    for root in roots:
        text = str(root)
        if not text or text == os.sep:
            continue
        variants.add(text)
        variants.add(text.replace("\\", "/"))
        variants.add(text.replace("/", "\\"))
    return tuple(sorted(variants, key=len, reverse=True))


class ReceiptRedactor:
    """Recursive secret/path redaction applied before hash or export."""

    def __init__(
        self,
        *,
        sensitive_roots: tuple = (),
        allowed_evidence_keys: frozenset[str] = frozenset(),
    ) -> None:
        self._allowed = frozenset(str(k).lower() for k in allowed_evidence_keys)
        self._extra_prefixes = _root_variants(sensitive_roots)

    def _prefixes(self) -> tuple[str, ...]:
        # Home/profile/temp prefixes are resolved lazily so a redactor
        # built at import time still tracks the active HADES_HOME.
        return tuple(
            sorted(
                set(_redaction_prefixes()) | set(self._extra_prefixes),
                key=len,
                reverse=True,
            )
        )

    def _is_credential_key(self, key: str) -> bool:
        lowered = key.lower()
        return any(marker in lowered for marker in _CREDENTIAL_KEY_MARKERS)

    def redact_text(self, text: str) -> str:
        out = _BEARER_RE.sub(f"Bearer {_REDACTED}", str(text))
        out = _URL_USERINFO_RE.sub(rf"\g<1>{_REDACTED}@", out)
        out = _URL_QUERY_RE.sub(rf"\g<1>?{_REDACTED}", out)
        for prefix in self._prefixes():
            if prefix and prefix in out:
                out = out.replace(prefix, "<redacted>")
        return out

    def redact(self, value: object) -> object:
        if isinstance(value, dict):
            redacted: dict = {}
            for key, item in value.items():
                key_text = str(key)
                lowered = key_text.lower()
                if lowered in self._allowed:
                    redacted[key] = self.redact(item)
                elif self._is_credential_key(key_text):
                    redacted[key] = _REDACTED
                elif lowered in _MESSAGE_BODY_KEYS:
                    redacted[key] = f"{_REDACTED}:message-body"
                else:
                    redacted[key] = self.redact(item)
            return redacted
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            return self.redact_text(value)
        return value


# ---------------------------------------------------------------------------
# Export payload encoding/decoding and hash verification
# ---------------------------------------------------------------------------

_EXPORT_FORMAT = "hades-receipt-export"
_EXPORT_VERSION = 1

_VERIFICATION_INSTRUCTIONS = (
    "Every content_hash is sha256 over canonical JSON: UTF-8, sorted "
    "string keys, compact separators, NFC strings, RFC 3339 UTC "
    "timestamps, tuples as arrays. Rebuild each claim/evidence/artifact "
    "hash from its fields, then the receipt/observation hash from the "
    "nested hashes, and compare with the embedded values — or call "
    "agent.receipts.verify_export_hashes(path). Attestations prove "
    "provenance over a content hash only; they never prove a status, "
    "claim, or artifact content is true."
)


def _receipt_to_payload(receipt: Receipt) -> dict:
    import dataclasses

    return dataclasses.asdict(receipt)


def _observation_to_payload(observation: ReceiptObservation) -> dict:
    import dataclasses

    return dataclasses.asdict(observation)


def _attestation_to_payload(attestation: ReceiptAttestation) -> dict:
    import dataclasses

    payload = dataclasses.asdict(attestation)
    payload["role"] = "provenance only"
    return payload


def _claims_from_payload(items: list) -> tuple[ReceiptClaim, ...]:
    return tuple(
        ReceiptClaim(
            claim_id=d["claim_id"],
            claim_kind=d["claim_kind"],
            statement=d["statement"],
            expected_json=d["expected_json"],
            observed_json=d["observed_json"],
            evidence_ids=tuple(d["evidence_ids"]),
            artifact_ids=tuple(d["artifact_ids"]),
            required=d["required"],
            verdict=d["verdict"],
            uncertainty=tuple(d["uncertainty"]),
            content_hash=d["content_hash"],
        )
        for d in items
    )


def _evidence_from_payload(items: list) -> tuple[EvidenceDigest, ...]:
    return tuple(
        EvidenceDigest(
            evidence_id=d["evidence_id"],
            evidence_kind=d["evidence_kind"],
            source_ref=d["source_ref"],
            producer_id=d["producer_id"],
            observed_at=d["observed_at"],
            fresh_until=d["fresh_until"],
            summary=d["summary"],
            payload_hash=d["payload_hash"],
            artifact_ids=tuple(d["artifact_ids"]),
            content_hash=d["content_hash"],
        )
        for d in items
    )


def _artifacts_from_payload(items: list) -> tuple[ArtifactDigest, ...]:
    return tuple(
        ArtifactDigest(
            artifact_id=d["artifact_id"],
            source_kind=d["source_kind"],
            source_ref=d["source_ref"],
            display_name=d["display_name"],
            media_type=d["media_type"],
            size_bytes=d["size_bytes"],
            sha256=d["sha256"],
            mtime_ns=d["mtime_ns"],
            captured_at=d["captured_at"],
            content_hash=d["content_hash"],
        )
        for d in items
    )


def _receipt_from_payload(data: dict) -> Receipt:
    outcome = data["requested_outcome"]
    return Receipt(
        receipt_id=data["receipt_id"],
        source=ReceiptSourceKey(
            data["source"]["source_kind"], data["source"]["source_id"]
        ),
        subject_kind=data["subject_kind"],
        subject_id=data["subject_id"],
        session_id=data["session_id"],
        turn_id=data["turn_id"],
        mission_id=data["mission_id"],
        transaction_id=data["transaction_id"],
        requested_outcome=RequestedOutcome(
            outcome_kind=outcome["outcome_kind"],
            description=outcome["description"],
            constraints=tuple(outcome["constraints"]),
            producer_id=outcome["producer_id"],
            content_hash=outcome["content_hash"],
        ),
        status=data["status"],
        claims=_claims_from_payload(data["claims"]),
        evidence=_evidence_from_payload(data["evidence"]),
        artifacts=_artifacts_from_payload(data["artifacts"]),
        uncertainty=tuple(data["uncertainty"]),
        scorer_id=data["scorer_id"],
        scorer_version=data["scorer_version"],
        decided_at=data["decided_at"],
        content_hash=data["content_hash"],
    )


def _observation_from_payload(data: dict) -> ReceiptObservation:
    return ReceiptObservation(
        observation_id=data["observation_id"],
        receipt_id=data["receipt_id"],
        previous_observation_id=data["previous_observation_id"],
        status=data["status"],
        claims=_claims_from_payload(data["claims"]),
        evidence=_evidence_from_payload(data["evidence"]),
        artifacts=_artifacts_from_payload(data["artifacts"]),
        uncertainty=tuple(data["uncertainty"]),
        scorer_id=data["scorer_id"],
        scorer_version=data["scorer_version"],
        observed_at=data["observed_at"],
        content_hash=data["content_hash"],
    )


def _attestation_body(attestation: ReceiptAttestation) -> dict:
    return {
        "target_kind": attestation.target_kind,
        "target_id": attestation.target_id,
        "target_content_hash": attestation.target_content_hash,
        "provider_id": attestation.provider_id,
        "key_id": attestation.key_id,
        "algorithm": attestation.algorithm,
        "signature_b64": attestation.signature_b64,
        "signed_at": attestation.signed_at,
        "verification_state": attestation.verification_state,
    }


def verify_export_hashes(path: Path | str) -> bool:
    """Recompute every canonical hash inside an exported receipt file.

    Returns ``True`` only when the receipt, each observation, and each
    attestation in the export match their recomputed canonical content
    hashes. Any parse error, missing field, or mismatch returns
    ``False`` — verification never guesses.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        receipt = _receipt_from_payload(data["receipt"])
        _validate_receipt(receipt)
        verification = data.get("verification") or {}
        if verification.get("receipt_content_hash") != receipt.content_hash:
            return False
        for payload in data.get("observations", []):
            observation = _observation_from_payload(payload)
            for claim in observation.claims:
                if _rebuild_claim(claim) != claim:
                    return False
            for evidence in observation.evidence:
                if _rebuild_evidence(evidence) != evidence:
                    return False
            for artifact in observation.artifacts:
                if _rebuild_artifact(artifact) != artifact:
                    return False
            rebuilt = build_observation(
                receipt_id=observation.receipt_id,
                previous_observation_id=observation.previous_observation_id,
                status=observation.status,
                claims=observation.claims,
                evidence=observation.evidence,
                artifacts=observation.artifacts,
                uncertainty=observation.uncertainty,
                scorer_id=observation.scorer_id,
                scorer_version=observation.scorer_version,
                observed_at=observation.observed_at,
            )
            # Migrated legacy observation IDs are the compatibility
            # exception; the canonical hash must still recompute.
            if rebuilt.content_hash != observation.content_hash:
                return False
        for payload in data.get("attestations", []):
            fields = {
                key: payload[key]
                for key in (
                    "target_kind",
                    "target_id",
                    "target_content_hash",
                    "provider_id",
                    "key_id",
                    "algorithm",
                    "signature_b64",
                    "signed_at",
                    "verification_state",
                )
            }
            if canonical_content_hash(fields) != payload["content_hash"]:
                return False
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------

_SAFE_EXTENSION_RE = re.compile(r"\.([A-Za-z0-9]{1,10})$")


def _sanitized_extension(display_name: str) -> str:
    match = _SAFE_EXTENSION_RE.search(str(display_name or ""))
    return f".{match.group(1).lower()}" if match else ""


def _profile_home() -> Path | None:
    try:
        from hades_constants import get_hades_home

        return Path(get_hades_home()).expanduser().resolve()
    except Exception:
        return None


class ReceiptExporter:
    """Redacted public/local export and safe artifact bundles."""

    def __init__(
        self,
        store: ReceiptStore,
        *,
        redactor: ReceiptRedactor | None = None,
        default_redaction: str = "public",
        allowed_roots: tuple = (),
        signing: "ReceiptSigningService | None" = None,
    ) -> None:
        if default_redaction not in ("public", "local"):
            raise ValueError(
                f"default_redaction must be 'public' or 'local', got "
                f"{default_redaction!r}"
            )
        self._store = store
        self._redactor = redactor if redactor is not None else ReceiptRedactor()
        self._default_redaction = default_redaction
        self._allowed_roots = tuple(
            Path(root).expanduser().resolve() for root in allowed_roots
        )
        self._signing = signing

    # ── Locator access (raw paths never leave this module unredacted) ──

    def _file_locators(self, artifact_id: str) -> tuple[Path, ...]:
        def _do(conn: sqlite3.Connection) -> tuple[Path, ...]:
            paths: list[Path] = []
            for row in conn.execute(
                "SELECT locator_json FROM artifact_locations "
                "WHERE artifact_id = ? ORDER BY created_at, location_id",
                (artifact_id,),
            ):
                try:
                    locator = json.loads(row["locator_json"])
                except ValueError:
                    continue
                if isinstance(locator, dict) and locator.get("kind") == "file":
                    raw = str(locator.get("path") or "")
                    if raw:
                        paths.append(Path(raw))
            return tuple(paths)

        return self._store._db._execute_read(_do)

    def _profile_relative_locators(
        self, artifacts: tuple[ArtifactDigest, ...]
    ) -> list[dict]:
        home = _profile_home()
        if home is None:
            return []
        entries: list[dict] = []
        for artifact in artifacts:
            for raw in self._file_locators(artifact.artifact_id):
                try:
                    resolved = raw.expanduser().resolve()
                    relative = resolved.relative_to(home)
                except (OSError, ValueError):
                    # Boundary check failed: a locator outside the
                    # profile home never enters an export.
                    continue
                entries.append(
                    {
                        "artifact_id": artifact.artifact_id,
                        "path": relative.as_posix(),
                    }
                )
        return entries

    # ── Export ──

    def export(
        self,
        receipt_id: str,
        output: Path | str,
        *,
        redaction: str | None = None,
        bundle_artifacts: bool = False,
        sign: bool = False,
    ) -> Path:
        mode = redaction or self._default_redaction
        if mode not in ("public", "local"):
            raise ReceiptExportError(
                f"redaction must be 'public' or 'local', got {mode!r}"
            )
        receipt = self._store.get(receipt_id)
        if receipt is None:
            raise ReceiptExportError(f"unknown receipt {receipt_id!r}")
        observations = self._store.observations(receipt_id)

        if sign:
            self._sign_for_export(receipt)

        attestations: list[ReceiptAttestation] = list(
            self._store.list_attestations(receipt_id)
        )
        for observation in observations:
            attestations.extend(
                self._store.list_attestations(observation.observation_id)
            )

        payload: dict = {
            "format": _EXPORT_FORMAT,
            "version": _EXPORT_VERSION,
            "redaction": mode,
            "exported_at": _now(),
            "receipt": _receipt_to_payload(receipt),
            "observations": [
                _observation_to_payload(observation)
                for observation in observations
            ],
            "attestations": [
                _attestation_to_payload(attestation)
                for attestation in attestations
            ],
            "verification": {
                "algorithm": "sha256 canonical JSON",
                "receipt_content_hash": receipt.content_hash,
                "instructions": _VERIFICATION_INSTRUCTIONS,
            },
        }
        if mode == "local":
            all_artifacts = list(receipt.artifacts)
            for observation in observations:
                all_artifacts.extend(observation.artifacts)
            seen: set[str] = set()
            unique = tuple(
                a
                for a in all_artifacts
                if a.artifact_id not in seen and not seen.add(a.artifact_id)
            )
            payload["profile_relative_locators"] = (
                self._profile_relative_locators(unique)
            )

        # Defense in depth: the persisted canonical content is already
        # redacted; a leak that slipped past producers is scrubbed here
        # and would truthfully fail hash verification.
        payload = self._redactor.redact(payload)

        output_path = Path(output)
        if bundle_artifacts:
            self._write_bundle(output_path, payload, receipt)
        else:
            self._write_file(
                output_path,
                json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, indent=2
                ).encode("utf-8"),
            )
        return output_path

    def _sign_for_export(self, receipt: Receipt) -> None:
        if self._signing is None:
            raise ReceiptExportError(
                "signed export requested but no signing service is configured"
            )
        try:
            attestation = self._signing.sign(receipt)
        except SigningUnavailableError as exc:
            raise ReceiptExportError(
                f"required signing is unavailable; refusing signed export: {exc}"
            ) from exc
        if attestation is None:
            logger.warning(
                "receipt %s exported unsigned: optional signing provider "
                "unavailable",
                receipt.receipt_id,
            )

    # ── Safe output writing ──

    @staticmethod
    def _write_file(path: Path, data: bytes) -> None:
        if path.is_symlink():
            raise ReceiptExportError(
                f"refusing to write export through symlink {path.name!r}"
            )
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_TRUNC
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        try:
            fd = os.open(str(path), flags, 0o600)
        except OSError as exc:
            raise ReceiptExportError(
                f"cannot open export output: {type(exc).__name__}"
            ) from exc
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def _write_bundle(
        self, output: Path, payload: dict, receipt: Receipt
    ) -> None:
        if output.is_symlink():
            raise ReceiptExportError(
                f"refusing to write bundle through symlink {output.name!r}"
            )
        temp = output.with_name(f"{output.name}.tmp-{os.getpid()}")
        try:
            with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "receipt.json",
                    json.dumps(
                        payload, ensure_ascii=False, sort_keys=True, indent=2
                    ),
                )
                for artifact in receipt.artifacts:
                    name = (
                        f"artifacts/{artifact.artifact_id}"
                        f"{_sanitized_extension(artifact.display_name)}"
                    )
                    archive.writestr(name, self._read_verified(artifact))
            if output.is_symlink():
                raise ReceiptExportError(
                    f"refusing to write bundle through symlink {output.name!r}"
                )
            os.replace(temp, output)
        except BaseException:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _read_verified(self, artifact: ArtifactDigest) -> bytes:
        """Reopen and re-hash artifact bytes while copying into a bundle."""
        import hashlib

        locators = self._file_locators(artifact.artifact_id)
        if not locators:
            raise ReceiptExportError(
                f"artifact {artifact.artifact_id} has no durable file "
                "location to bundle"
            )
        last_error: str = "no readable location"
        for raw in locators:
            try:
                resolved = raw.expanduser().resolve()
            except OSError:
                continue
            if self._allowed_roots and not any(
                resolved == root or root in resolved.parents
                for root in self._allowed_roots
            ):
                last_error = "location is outside the allowed export roots"
                continue
            try:
                data = resolved.read_bytes()
            except OSError as exc:
                last_error = f"cannot read location: {type(exc).__name__}"
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest != artifact.sha256 or len(data) != artifact.size_bytes:
                raise ReceiptExportError(
                    f"artifact {artifact.artifact_id} bytes differ from the "
                    "recorded digest (hash mismatch); refusing to bundle"
                )
            return data
        raise ReceiptExportError(
            f"artifact {artifact.artifact_id} cannot be bundled: {last_error}"
        )


# ---------------------------------------------------------------------------
# Service-gated signing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignatureMaterial:
    """Provider-produced signature bytes over one content hash."""

    key_id: str
    algorithm: str
    signature_b64: str


@runtime_checkable
class ReceiptSigner(Protocol):
    """Provenance signer over canonical content hashes."""

    provider_id: str

    def sign(self, content_hash: str) -> SignatureMaterial: ...

    def verify(self, content_hash: str, material: SignatureMaterial) -> bool: ...


SignerFactory = Callable[[dict], ReceiptSigner]

_SIGNER_REGISTRY: dict[str, tuple[SignerFactory, Callable[[dict], bool]]] = {}

_PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def register_receipt_signer(
    provider_id: str,
    factory: SignerFactory,
    check_fn: Callable[[dict], bool],
) -> None:
    """Register a signer factory behind the service gate.

    Registration alone loads nothing: the factory runs only when config
    names ``provider_id`` and ``check_fn(config)`` returns true.
    """
    if not isinstance(provider_id, str) or not _PROVIDER_ID_RE.fullmatch(
        provider_id
    ):
        raise ValueError(
            f"provider_id must be a bounded identifier, got {provider_id!r}"
        )
    if not callable(factory) or not callable(check_fn):
        raise TypeError("factory and check_fn must be callable")
    _SIGNER_REGISTRY[provider_id] = (factory, check_fn)


def unregister_receipt_signer(provider_id: str) -> None:
    _SIGNER_REGISTRY.pop(provider_id, None)


@dataclass(frozen=True)
class SignatureVerification:
    """Truthful outcome of one explicit attestation verification."""

    valid: bool
    state: str
    detail: str


class ReceiptSigningService:
    """Signs canonical target hashes and verifies attestations.

    A valid signature proves provenance over a content hash. It never
    changes a status, claim verdict, uncertainty, freshness, or scorer
    result, and it never proves artifact contents or claims are true.
    """

    def __init__(
        self,
        store: ReceiptStore | None = None,
        *,
        provider_id: str = "",
        required: bool = False,
        signer: ReceiptSigner | None = None,
    ) -> None:
        self.store = store
        self._provider_id = provider_id
        self._required = bool(required)
        self._signer = signer

    @classmethod
    def from_config(
        cls, config: dict | None, *, store: ReceiptStore | None = None
    ) -> "ReceiptSigningService":
        """Build the gated service. No provider loads until config names
        it and its registered ``check_fn`` accepts the config."""
        signing_cfg = ((config or {}).get("receipts") or {}).get("signing") or {}
        provider_id = str(signing_cfg.get("provider") or "")
        required = bool(signing_cfg.get("required", False))
        signer: ReceiptSigner | None = None
        if provider_id:
            entry = _SIGNER_REGISTRY.get(provider_id)
            if entry is not None:
                factory, check_fn = entry
                try:
                    approved = bool(check_fn(config or {}))
                except Exception:
                    logger.warning(
                        "receipt signer %r check_fn raised; provider not "
                        "loaded",
                        provider_id,
                    )
                    approved = False
                if approved:
                    try:
                        signer = factory(config or {})
                    except Exception:
                        logger.warning(
                            "receipt signer %r factory failed; provider not "
                            "loaded",
                            provider_id,
                        )
                        signer = None
        return cls(
            store, provider_id=provider_id, required=required, signer=signer
        )

    @property
    def available(self) -> bool:
        return self._signer is not None

    @property
    def required(self) -> bool:
        return self._required

    # ── Signing ──

    @staticmethod
    def _target_facts(target: object) -> tuple[str, str, str]:
        if isinstance(target, Receipt):
            return "receipt", target.receipt_id, target.content_hash
        if isinstance(target, ReceiptObservation):
            return "observation", target.observation_id, target.content_hash
        raise TypeError(
            "sign/verify targets must be a Receipt or ReceiptObservation, "
            f"got {type(target).__name__}"
        )

    def sign(self, target: object) -> ReceiptAttestation | None:
        """Sign the existing canonical target hash, append an immutable
        attestation, and return it.

        Optional signing failure returns ``None`` with an operator
        warning — the receipt stays truthfully unsigned. Required
        signing raises :class:`SigningUnavailableError`, which prevents
        signed export/consumer projection but can never change a receipt
        status.
        """
        target_kind, target_id, target_hash = self._target_facts(target)
        if self.store is None:
            raise ReceiptSecurityError(
                "signing service has no receipt store to append attestations"
            )
        if self._signer is None:
            if self._required:
                raise SigningUnavailableError(
                    f"required signing provider {self._provider_id!r} is not "
                    "available"
                )
            logger.warning(
                "receipt signing skipped for %s %s: provider %r unavailable; "
                "the receipt stays truthfully unsigned",
                target_kind,
                target_id,
                self._provider_id,
            )
            return None
        try:
            material = self._signer.sign(target_hash)
        except Exception as exc:
            if self._required:
                raise SigningUnavailableError(
                    f"required signing provider {self._provider_id!r} failed "
                    f"to sign: {type(exc).__name__}"
                ) from exc
            logger.warning(
                "receipt signing failed for %s %s (%s); the receipt stays "
                "truthfully unsigned",
                target_kind,
                target_id,
                type(exc).__name__,
            )
            return None
        # Idempotent replay: an identical provider signature over the
        # same target hash returns the stored attestation.
        for existing in self.store.list_attestations(target_id):
            if (
                existing.provider_id == self._provider_id
                and existing.target_content_hash == target_hash
                and existing.signature_b64 == material.signature_b64
                and existing.key_id == material.key_id
                and existing.algorithm == material.algorithm
            ):
                return existing
        body = {
            "target_kind": target_kind,
            "target_id": target_id,
            "target_content_hash": target_hash,
            "provider_id": self._provider_id,
            "key_id": material.key_id,
            "algorithm": material.algorithm,
            "signature_b64": material.signature_b64,
            "signed_at": _now(),
            "verification_state": "provider_signed",
        }
        content_hash = canonical_content_hash(body)
        attestation = ReceiptAttestation(
            attestation_id=f"att_{hash_hex(content_hash)}",
            content_hash=content_hash,
            **body,
        )
        return self.store.append_attestation(attestation)

    # ── Explicit verification ──

    def verify(self, attestation: ReceiptAttestation) -> SignatureVerification:
        if not isinstance(attestation, ReceiptAttestation):
            raise TypeError(
                "expected a ReceiptAttestation, got "
                f"{type(attestation).__name__}"
            )
        recomputed = canonical_content_hash(_attestation_body(attestation))
        if recomputed != attestation.content_hash:
            return SignatureVerification(
                valid=False,
                state="forged",
                detail="attestation content does not match its own hash",
            )
        if self.store is None:
            return SignatureVerification(
                valid=False,
                state="unavailable",
                detail="no receipt store to resolve the attestation target",
            )
        if attestation.target_kind == "receipt":
            target = self.store.get(attestation.target_id)
            stored_hash = target.content_hash if target is not None else None
        elif attestation.target_kind == "observation":
            observation = self.store.get_observation(attestation.target_id)
            stored_hash = (
                observation.content_hash if observation is not None else None
            )
        else:
            stored_hash = None
        if stored_hash is None:
            return SignatureVerification(
                valid=False,
                state="unknown_target",
                detail=(
                    f"attestation target {attestation.target_kind}:"
                    f"{attestation.target_id} does not exist"
                ),
            )
        if stored_hash != attestation.target_content_hash:
            return SignatureVerification(
                valid=False,
                state="target_mismatch",
                detail=(
                    "attestation hash does not match the stored canonical "
                    "content hash — a signature is bound to exactly one "
                    "content hash"
                ),
            )
        if self._signer is None or attestation.provider_id != self._provider_id:
            return SignatureVerification(
                valid=False,
                state="unavailable",
                detail=(
                    f"provider {attestation.provider_id!r} is not the loaded "
                    "signing provider; explicit verification requires it"
                ),
            )
        material = SignatureMaterial(
            key_id=attestation.key_id,
            algorithm=attestation.algorithm,
            signature_b64=attestation.signature_b64,
        )
        try:
            cryptographically_valid = bool(
                self._signer.verify(attestation.target_content_hash, material)
            )
        except Exception as exc:
            return SignatureVerification(
                valid=False,
                state="invalid",
                detail=f"provider verification raised {type(exc).__name__}",
            )
        if not cryptographically_valid:
            return SignatureVerification(
                valid=False,
                state="invalid",
                detail="signature bytes do not verify over the target hash",
            )
        return SignatureVerification(
            valid=True,
            state="verified",
            detail=(
                "signature verifies over the content hash — provenance "
                "only, never truth"
            ),
        )


# ---------------------------------------------------------------------------
# Explicit retention with tombstones and holds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionHold:
    """One reason a receipt must not be deleted."""

    receipt_id: str
    kind: str  # "mission" | "transaction" | "legal" | "user"
    reason: str


@dataclass(frozen=True)
class RetentionPlan:
    """Exact deletion candidates and blockers at one point in time."""

    plan_id: str
    plan_hash: str
    generated_at: str
    retention_cutoff: str
    locator_cutoff: str
    receipt_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    attestation_ids: tuple[str, ...]
    artifact_location_ids: tuple[str, ...]
    blockers: tuple[RetentionHold, ...]


@dataclass(frozen=True)
class RetentionPruneResult:
    plan_id: str
    deleted_receipts: int
    deleted_observations: int
    deleted_attestations: int
    deleted_artifact_locations: int
    deleted_artifact_bytes: int
    tombstones: int
    already_deleted: int


_MISSION_TERMINAL_STATUSES = frozenset(
    {"completed", "succeeded", "failed", "cancelled"}
)
_TRANSACTION_TERMINAL_PHASES = frozenset(
    {"committed", "compensated", "failed", "rolled_back", "cancelled", "aborted"}
)


def _cutoff(now_ts: str, days: int) -> str:
    parsed = datetime.fromisoformat(now_ts.replace("Z", "+00:00"))
    return normalize_utc_timestamp(parsed - timedelta(days=days))


class ReceiptRetentionService:
    """Explicit, bounded retention over one profile's receipt store.

    ``plan(now)`` returns the exact receipt/observation/attestation/
    artifact-location IDs and blockers. ``prune(plan_id, expected_hash)``
    revalidates the plan, refuses active mission/transaction/legal/user
    holds, deletes expired raw artifact locators before receipt rows,
    and atomically inserts deletion tombstones. It never runs implicitly
    during a live turn and never deletes artifact bytes outside the
    configured receipt artifact directory.
    """

    def __init__(
        self,
        store: ReceiptStore,
        *,
        retention_days: int = 365,
        locator_retention_days: int = 90,
        holds: object = (),
        workflows_db_path: Path | None = None,
        artifact_dir: Path | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        if not 1 <= int(retention_days) <= 3650:
            raise ValueError("retention_days must be in 1..3650")
        if not 1 <= int(locator_retention_days) <= int(retention_days):
            raise ValueError(
                "locator_retention_days must be in 1..retention_days"
            )
        self._store = store
        self._retention_days = int(retention_days)
        self._locator_days = int(locator_retention_days)
        self._holds = holds
        self._workflows_db_path = (
            Path(workflows_db_path) if workflows_db_path is not None else None
        )
        self._artifact_dir = (
            Path(artifact_dir).expanduser().resolve()
            if artifact_dir is not None
            else None
        )
        self._now = now if now is not None else _now
        self._issued: dict[str, RetentionPlan] = {}

    # ── Hold evaluation ──

    def _explicit_holds(self) -> tuple[RetentionHold, ...]:
        source = self._holds
        items = source() if callable(source) else source
        holds: list[RetentionHold] = []
        for item in items or ():
            if isinstance(item, RetentionHold):
                holds.append(item)
        return tuple(holds)

    def _resolve_workflows_path(self) -> Path | None:
        if self._workflows_db_path is not None:
            return self._workflows_db_path
        try:
            from hades_cli.workflows_db import workflows_db_path

            return workflows_db_path()
        except Exception:
            return None

    def _mission_active(self, mission_id: str) -> bool:
        """Existence-guarded: activates when the vertical slice lands."""
        path = self._resolve_workflows_path()
        if path is None or not Path(path).exists():
            return False
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            return False
        try:
            conn.row_factory = sqlite3.Row
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='missions'"
            ).fetchone()
            if exists is None:
                return False
            row = conn.execute(
                "SELECT status FROM missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
        except sqlite3.Error:
            return False
        finally:
            conn.close()
        if row is None:
            return False
        return str(row["status"]) not in _MISSION_TERMINAL_STATUSES

    def _transaction_active(self, transaction_id: str) -> bool:
        """Existence-guarded: activates when the vertical slice lands."""

        def _do(conn: sqlite3.Connection) -> bool:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='effect_transactions'"
            ).fetchone()
            if exists is None:
                return False
            row = conn.execute(
                "SELECT phase FROM effect_transactions "
                "WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
            if row is None:
                return False
            return str(row["phase"]) not in _TRANSACTION_TERMINAL_PHASES

        try:
            return bool(self._store._db._execute_read(_do))
        except Exception:
            return False

    def _blockers_for(self, candidates: list[dict]) -> tuple[RetentionHold, ...]:
        blockers: list[RetentionHold] = []
        candidate_ids = {row["receipt_id"] for row in candidates}
        for hold in self._explicit_holds():
            if hold.receipt_id in candidate_ids:
                blockers.append(hold)
        for row in candidates:
            mission_id = row.get("mission_id")
            if mission_id and self._mission_active(str(mission_id)):
                blockers.append(
                    RetentionHold(
                        row["receipt_id"],
                        "mission",
                        f"mission {mission_id} is still active",
                    )
                )
            transaction_id = row.get("transaction_id")
            if transaction_id and self._transaction_active(str(transaction_id)):
                blockers.append(
                    RetentionHold(
                        row["receipt_id"],
                        "transaction",
                        f"transaction {transaction_id} is still active",
                    )
                )
        return tuple(blockers)

    # ── Plan ──

    def _compute_plan(self, now_ts: str) -> RetentionPlan:
        retention_cutoff = _cutoff(now_ts, self._retention_days)
        locator_cutoff = _cutoff(now_ts, self._locator_days)

        def _read(conn: sqlite3.Connection):
            receipts = [
                dict(row)
                for row in conn.execute(
                    "SELECT receipt_id, source_kind, source_id, mission_id, "
                    "transaction_id, decided_at, artifacts_json FROM receipts "
                    "ORDER BY decided_at, receipt_id"
                )
            ]
            latest_obs = {
                row["receipt_id"]: row["latest"]
                for row in conn.execute(
                    "SELECT receipt_id, MAX(observed_at) AS latest "
                    "FROM receipt_observations GROUP BY receipt_id"
                )
            }
            locations = [
                dict(row)
                for row in conn.execute(
                    "SELECT location_id, artifact_id, locator_json, "
                    "created_at FROM artifact_locations "
                    "ORDER BY created_at, location_id"
                )
            ]
            return receipts, latest_obs, locations

        receipts, latest_obs, locations = self._store._db._execute_read(_read)

        candidates = [
            row
            for row in receipts
            if row["decided_at"] < retention_cutoff
            and (latest_obs.get(row["receipt_id"]) or "") < retention_cutoff
        ]
        blockers = self._blockers_for(candidates)
        blocked_ids = {hold.receipt_id for hold in blockers}
        expired = [
            row for row in candidates if row["receipt_id"] not in blocked_ids
        ]
        expired_ids = tuple(row["receipt_id"] for row in expired)

        def _artifact_ids(row: dict) -> set[str]:
            try:
                parsed = json.loads(row.get("artifacts_json") or "[]")
            except ValueError:
                return set()
            return {
                str(item.get("artifact_id"))
                for item in parsed
                if isinstance(item, dict) and item.get("artifact_id")
            }

        surviving_artifacts: set[str] = set()
        expired_artifacts: set[str] = set()
        for row in receipts:
            target = (
                expired_artifacts
                if row["receipt_id"] in set(expired_ids)
                else surviving_artifacts
            )
            target |= _artifact_ids(row)

        location_ids: list[str] = []
        for location in locations:
            artifact_id = location["artifact_id"]
            if artifact_id in surviving_artifacts:
                continue
            if artifact_id in expired_artifacts:
                # Raw locators for a pruned receipt's artifacts never
                # outlive the receipt.
                location_ids.append(location["location_id"])
                continue
            try:
                locator = json.loads(location["locator_json"])
            except ValueError:
                locator = {}
            is_file = (
                isinstance(locator, dict) and locator.get("kind") == "file"
            )
            if is_file and location["created_at"] < locator_cutoff:
                location_ids.append(location["location_id"])

        def _ids(conn: sqlite3.Connection):
            observation_ids: list[str] = []
            attestation_ids: list[str] = []
            for receipt_id in expired_ids:
                obs = [
                    row["observation_id"]
                    for row in conn.execute(
                        "SELECT observation_id FROM receipt_observations "
                        "WHERE receipt_id = ? ORDER BY inserted_at, rowid",
                        (receipt_id,),
                    )
                ]
                observation_ids.extend(obs)
                for target_id in [receipt_id, *obs]:
                    attestation_ids.extend(
                        row["attestation_id"]
                        for row in conn.execute(
                            "SELECT attestation_id FROM receipt_attestations "
                            "WHERE target_id = ? ORDER BY attestation_id",
                            (target_id,),
                        )
                    )
            return tuple(observation_ids), tuple(attestation_ids)

        observation_ids, attestation_ids = self._store._db._execute_read(_ids)

        hash_body = {
            "retention_days": self._retention_days,
            "locator_retention_days": self._locator_days,
            "receipt_ids": sorted(expired_ids),
            "observation_ids": sorted(observation_ids),
            "attestation_ids": sorted(attestation_ids),
            "artifact_location_ids": sorted(location_ids),
            "blockers": sorted(
                [hold.receipt_id, hold.kind, hold.reason] for hold in blockers
            ),
        }
        plan_hash = canonical_content_hash(hash_body)
        return RetentionPlan(
            plan_id=f"rpl_{hash_hex(plan_hash)}",
            plan_hash=plan_hash,
            generated_at=now_ts,
            retention_cutoff=retention_cutoff,
            locator_cutoff=locator_cutoff,
            receipt_ids=expired_ids,
            observation_ids=observation_ids,
            attestation_ids=attestation_ids,
            artifact_location_ids=tuple(location_ids),
            blockers=blockers,
        )

    def plan(self, now: str | datetime | None = None) -> RetentionPlan:
        if now is None:
            now_ts = normalize_utc_timestamp(self._now())
        else:
            now_ts = normalize_utc_timestamp(now)
        plan = self._compute_plan(now_ts)
        self._issued[plan.plan_id] = plan
        return plan

    # ── Prune ──

    def prune(self, plan_id: str, expected_hash: str) -> RetentionPruneResult:
        plan = self._issued.get(plan_id)
        if plan is None:
            # Fresh process: revalidate by full recomputation. Any drift
            # since the plan was generated changes the hash and refuses.
            plan = self._compute_plan(normalize_utc_timestamp(self._now()))
            if plan.plan_id != plan_id:
                raise RetentionPlanMismatch(
                    "unknown or stale retention plan; re-run retention-plan "
                    "and confirm the new hash"
                )
        if plan.plan_hash != expected_hash:
            raise RetentionPlanMismatch(
                "confirmed plan hash does not match the current retention "
                "plan; re-run retention-plan and confirm the exact hash"
            )

        # Revalidate holds NOW: a hold added after planning refuses the
        # prune outright.
        def _present(conn: sqlite3.Connection) -> list[dict]:
            rows: list[dict] = []
            for receipt_id in plan.receipt_ids:
                row = conn.execute(
                    "SELECT receipt_id, mission_id, transaction_id "
                    "FROM receipts WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()
                if row is not None:
                    rows.append(dict(row))
            return rows

        present = self._store._db._execute_read(_present)
        active_holds = self._blockers_for(present)
        if active_holds:
            details = "; ".join(
                f"{hold.receipt_id}: {hold.kind} ({hold.reason})"
                for hold in active_holds
            )
            raise RetentionHoldError(
                f"prune refused by active holds: {details}"
            )

        counts = self._store._retention_delete(
            receipt_ids=plan.receipt_ids,
            artifact_location_ids=plan.artifact_location_ids,
            deleted_at=normalize_utc_timestamp(self._now()),
            reason=f"retention_expired:{plan.plan_id}",
        )
        deleted_bytes = self._delete_bytes(counts.pop("deleted_locator_paths"))
        return RetentionPruneResult(
            plan_id=plan.plan_id,
            deleted_receipts=counts["deleted_receipts"],
            deleted_observations=counts["deleted_observations"],
            deleted_attestations=counts["deleted_attestations"],
            deleted_artifact_locations=counts["deleted_artifact_locations"],
            deleted_artifact_bytes=deleted_bytes,
            tombstones=counts["tombstones"],
            already_deleted=counts["already_deleted"],
        )

    def _delete_bytes(self, locator_paths: list[str]) -> int:
        """Unlink raw byte payloads ONLY inside the configured receipt
        artifact directory; everything else stays on disk untouched."""
        if self._artifact_dir is None:
            return 0
        deleted = 0
        for raw in locator_paths:
            path = Path(raw)
            try:
                if path.is_symlink():
                    continue
                resolved = path.expanduser().resolve()
            except OSError:
                continue
            if not (
                resolved == self._artifact_dir
                or self._artifact_dir in resolved.parents
            ):
                continue
            try:
                os.unlink(resolved)
                deleted += 1
            except FileNotFoundError:
                continue
            except OSError:
                logger.warning(
                    "retention could not delete artifact bytes at a pruned "
                    "locator; the row is gone but the file remains"
                )
        return deleted
