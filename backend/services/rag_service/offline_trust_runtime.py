"""Opt-in runtime composition for signed offline time and revocation evidence."""
from __future__ import annotations

import base64
import binascii
import re
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .trusted_state import (
    EligibilityDecision,
    OfflineTrustStore,
    TrustedStateError,
    read_signed_record,
)


class OfflineTrustRuntimeError(RuntimeError):
    """Offline trust is enabled but cannot establish fail-closed runtime state."""


_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")


def _projected_file(path: Path, label: str) -> Path:
    """Allow one projected-volume file symlink that remains inside its mount."""
    path = Path(path)
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise OfflineTrustRuntimeError("%s directory is missing or unsafe" % label)
    try:
        mount = parent.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(mount)
    except (OSError, RuntimeError, ValueError) as exc:
        raise OfflineTrustRuntimeError("%s is missing or escapes its mount" % label) from exc
    if not resolved.is_file():
        raise OfflineTrustRuntimeError("%s is not a regular file" % label)
    return resolved


def _public_key(path: Path, label: str) -> Ed25519PublicKey:
    path = _projected_file(path, label + " public key")
    try:
        raw = path.read_bytes()
        try:
            key = serialization.load_pem_public_key(raw)
        except ValueError:
            key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(raw.strip(), validate=True)
            )
    except (OSError, ValueError, TypeError, binascii.Error) as exc:
        raise OfflineTrustRuntimeError("%s public key is malformed" % label) from exc
    if not isinstance(key, Ed25519PublicKey):
        raise OfflineTrustRuntimeError("%s public key must be Ed25519" % label)
    return key


def _required(settings: Any, name: str) -> Any:
    value = getattr(settings, name, None)
    if value is None or value == "":
        raise OfflineTrustRuntimeError("offline trust setting %s is required" % name)
    return value


class OfflineTrustRuntime:
    """Loads signed evidence and gates every active-release retrieval."""

    def __init__(
        self,
        *,
        store: OfflineTrustStore,
        active_release_digest: str,
        release_signing_key_id: str,
        max_snapshot_age_seconds: int,
    ) -> None:
        self.store = store
        self.active_release_digest = active_release_digest
        self.release_signing_key_id = release_signing_key_id
        self.max_snapshot_age_seconds = max_snapshot_age_seconds

    @classmethod
    def from_settings(cls, settings: Any, active_release: Any) -> "OfflineTrustRuntime":
        if active_release is None:
            raise OfflineTrustRuntimeError(
                "offline trust requires an activation-managed corpus release"
            )
        event_id = _required(settings, "corpus_event_id")
        site_id = _required(settings, "offline_trust_site_id")
        node_id = _required(settings, "node_id")
        state_path = Path(_required(settings, "offline_trust_state_path"))
        boot_path = Path(_required(settings, "offline_trust_boot_id_path"))
        boot_path = _projected_file(boot_path, "boot identity")
        try:
            boot_id = boot_path.read_text(encoding="ascii").strip().lower()
        except (OSError, UnicodeError) as exc:
            raise OfflineTrustRuntimeError("boot identity cannot be read") from exc
        trust_generation = _required(settings, "offline_trust_generation")
        max_age = _required(settings, "offline_trust_max_snapshot_age_seconds")
        if type(trust_generation) is not int or trust_generation < 1 \
                or type(max_age) is not int or max_age < 1:
            raise OfflineTrustRuntimeError("offline trust generation and age must be positive")
        store = OfflineTrustStore(
            state_path,
            event_id=event_id,
            site_id=site_id,
            node_id=node_id,
            trust_generation=trust_generation,
            boot_id=boot_id,
            monotonic=time.monotonic,
        )
        time_key_id = _required(settings, "offline_trust_time_key_id")
        revocation_key_id = _required(settings, "offline_trust_revocation_key_id")
        time_key = _public_key(
            Path(_required(settings, "offline_trust_time_public_key_path")), "time authority"
        )
        revocation_key = _public_key(
            Path(_required(settings, "offline_trust_revocation_public_key_path")),
            "revocation authority",
        )
        try:
            anchor = read_signed_record(_projected_file(
                Path(_required(settings, "offline_trust_anchor_path")), "time anchor"
            ))
            if not store.has_current_time_anchor(anchor):
                store.accept_time_anchor(
                    anchor, expected_key_id=time_key_id, public_key=time_key
                )
            snapshot = read_signed_record(_projected_file(
                Path(_required(settings, "offline_trust_revocation_snapshot_path")),
                "revocation snapshot",
            ))
            if not store.has_current_revocation_snapshot(snapshot):
                store.accept_revocation_snapshot(
                    snapshot,
                    expected_key_id=revocation_key_id,
                    public_key=revocation_key,
                )
        except TrustedStateError as exc:
            raise OfflineTrustRuntimeError("offline trust evidence was rejected") from exc
        release_key_id = _required(settings, "offline_trust_release_signing_key_id")
        if not isinstance(release_key_id, str) or not _IDENTIFIER.fullmatch(release_key_id):
            raise OfflineTrustRuntimeError("release signing key identity is invalid")
        return cls(
            store=store,
            active_release_digest=active_release.status.digest,
            release_signing_key_id=release_key_id,
            max_snapshot_age_seconds=max_age,
        )

    def release_eligibility(self) -> EligibilityDecision:
        return self.store.assess_eligibility(
            purpose="serving",
            subjects={
                "release": [self.active_release_digest],
                "key": [self.release_signing_key_id],
            },
            max_snapshot_age_seconds=self.max_snapshot_age_seconds,
        )

    def sources_eligibility(
        self, result_metadata: Sequence[Mapping[str, Any]]
    ) -> EligibilityDecision:
        source_ids = []
        for metadata in result_metadata:
            if not isinstance(metadata, Mapping):
                return self._missing_source_decision()
            raw = metadata.get("source_ids", metadata.get("source_id"))
            if not isinstance(raw, str):
                return self._missing_source_decision()
            values = [item.strip() for item in raw.split(",") if item.strip()]
            if not values:
                return self._missing_source_decision()
            source_ids.extend(values)
        if not result_metadata:
            return self.store.assess_eligibility(
                purpose="serving", subjects={},
                max_snapshot_age_seconds=self.max_snapshot_age_seconds,
            )
        return self.store.assess_eligibility(
            purpose="serving",
            subjects={"source": sorted(set(source_ids))},
            max_snapshot_age_seconds=self.max_snapshot_age_seconds,
        )

    def _missing_source_decision(self) -> EligibilityDecision:
        health = self.store.assess_eligibility(
            purpose="serving", subjects={},
            max_snapshot_age_seconds=self.max_snapshot_age_seconds,
        )
        return EligibilityDecision(
            eligible=False,
            reason_codes=tuple(sorted(set(health.reason_codes + ("SOURCE_PROVENANCE_MISSING",)))),
            restrictions=health.restrictions,
            time_confidence=health.time_confidence,
            effective_time=health.effective_time,
            revocation_generation=health.revocation_generation,
        )

    def health_payload(self) -> Mapping[str, Any]:
        payload = dict(self.store.health_payload(
            max_snapshot_age_seconds=self.max_snapshot_age_seconds
        ))
        release = self.release_eligibility()
        payload["active_release_eligible"] = release.eligible
        payload["active_release_reasons"] = list(release.reason_codes)
        return payload
