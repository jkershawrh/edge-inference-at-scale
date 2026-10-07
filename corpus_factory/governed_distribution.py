"""Governed release evidence verification and safe activation materialization."""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Set

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from corpus_factory.evaluation_attestation import (
    EvaluationAttestationError,
    bytes_digest,
    object_digest,
    verify_attestation,
)
from corpus_factory.release_signing import ReleaseSigningError, verify_release_signature
from corpus_factory.validator import ContractValidationError, validate_instance


GOVERNED_PROFILE = "governed-v1"
LEGACY_PROFILE = "legacy-v1"
MAX_EVIDENCE_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024


class GovernedDistributionError(ValueError):
    """Raised before untrusted release material can enter activation intake."""


def _json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise GovernedDistributionError(f"{label} is missing or exceeds the evidence size limit")

    def reject_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise GovernedDistributionError(f"{label} contains a duplicate JSON key")
            value[key] = item
        return value

    try:
        result = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GovernedDistributionError(f"{label} is not valid JSON") from exc
    if not isinstance(result, dict):
        raise GovernedDistributionError(f"{label} must be a JSON object")
    return result


def _archive_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    if not members or len(members) > MAX_ARCHIVE_MEMBERS:
        raise GovernedDistributionError("corpus archive member count is invalid")
    total = 0
    names: set[str] = set()
    for member in members:
        path = PurePosixPath(member.name)
        if (
            not member.isfile()
            or path.is_absolute()
            or ".." in path.parts
            or member.name in names
        ):
            raise GovernedDistributionError("corpus archive contains an unsafe member")
        names.add(member.name)
        total += member.size
        if total > MAX_ARCHIVE_BYTES:
            raise GovernedDistributionError("corpus archive exceeds the extraction size limit")
    return members


def read_candidate_manifest(corpus_archive: Path | str) -> bytes:
    try:
        with tarfile.open(corpus_archive, "r:*") as archive:
            members = _archive_members(archive)
            matches = [member for member in members if member.name == "manifest.json"]
            if len(matches) != 1 or matches[0].size > MAX_EVIDENCE_BYTES:
                raise GovernedDistributionError("corpus archive must contain one bounded manifest.json")
            handle = archive.extractfile(matches[0])
            if handle is None:
                raise GovernedDistributionError("corpus manifest cannot be read")
            return handle.read()
    except (OSError, tarfile.TarError) as exc:
        raise GovernedDistributionError("corpus artifact is not a valid archive") from exc


def candidate_contract_profile(candidate: bytes) -> str:
    def reject_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise GovernedDistributionError("corpus manifest contains a duplicate JSON key")
            value[key] = item
        return value

    try:
        manifest = json.loads(candidate.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GovernedDistributionError("corpus manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("contract_profile") not in {
        GOVERNED_PROFILE, LEGACY_PROFILE
    }:
        raise GovernedDistributionError("corpus manifest has no supported contract profile")
    return manifest["contract_profile"]


@dataclass(frozen=True)
class GovernedReleaseVerifier:
    release_key_id: str
    release_public_key: Ed25519PublicKey
    attestation_keys: tuple[tuple[str, Ed25519PublicKey], ...]
    trusted_as_of: str
    minimum_attestation_revocation_generation: int
    revoked_attestation_ids: tuple[str, ...] = ()
    revoked_attestation_key_ids: tuple[str, ...] = ()

    def verify(self, corpus_archive: Path | str, signature_bundle_path: Path | str) -> dict[str, Any]:
        candidate = read_candidate_manifest(corpus_archive)
        if candidate_contract_profile(candidate) != GOVERNED_PROFILE:
            raise GovernedDistributionError("governed transfer requires a governed corpus manifest")
        bundle = _json_object(Path(signature_bundle_path), "signature bundle")
        required = {
            "schema_version", "contract_profile", "release_signature",
            "release_signing_authorization", "promotion_report", "evaluation_attestation",
        }
        if set(bundle) != required or bundle.get("schema_version") != "2.0.0" \
                or bundle.get("contract_profile") != GOVERNED_PROFILE:
            raise GovernedDistributionError("governed signature bundle is incomplete")
        authorization = bundle["release_signing_authorization"]
        promotion = bundle["promotion_report"]
        attestation = bundle["evaluation_attestation"]
        signature = bundle["release_signature"]
        if not all(isinstance(value, dict) for value in (authorization, promotion, attestation, signature)):
            raise GovernedDistributionError("governed signature bundle records must be objects")
        try:
            validate_instance(authorization, "release_signing_authorization")
            validate_instance(signature, "release_signature")
        except ContractValidationError as exc:
            raise GovernedDistributionError("governed release contract validation failed") from exc

        attestation_reference = authorization["evaluation_attestation"]
        if attestation_reference["revocation_generation"] < self.minimum_attestation_revocation_generation:
            raise GovernedDistributionError("evaluation revocation state is stale")
        public_key = dict(self.attestation_keys).get(attestation_reference["key_id"])
        if public_key is None:
            raise GovernedDistributionError("evaluation attestation key is not trusted")
        try:
            verify_attestation(
                attestation, promotion, public_key, as_of=self.trusted_as_of,
                revoked_attestation_ids=self.revoked_attestation_ids,
                revoked_key_ids=self.revoked_attestation_key_ids,
            )
        except EvaluationAttestationError as exc:
            raise GovernedDistributionError("signed evaluation evidence is invalid") from exc

        candidate_digest = bytes_digest(candidate)
        expected_report = {"report_id": promotion.get("report_id"), "digest": object_digest(promotion)}
        expected_attestation = {
            "attestation_id": attestation.get("attestation_id"),
            "digest": object_digest(attestation),
            "key_id": attestation.get("signing_key", {}).get("key_id"),
            "revocation_generation": attestation.get("revocation_snapshot", {}).get("generation"),
        }
        expected_bindings = {
            "evaluation_report_digest": object_digest(promotion),
            "approval_attestation_digests": [object_digest(attestation)],
        }
        if candidate_digest != promotion.get("binding", {}).get("release_digest") \
                or candidate_digest != attestation.get("subject", {}).get("release_digest"):
            raise GovernedDistributionError("candidate is not covered by signed evaluation evidence")
        if authorization["promotion_report"] != expected_report \
                or authorization["evaluation_attestation"] != expected_attestation \
                or authorization["release_signature_bindings"] != expected_bindings:
            raise GovernedDistributionError("authorization does not bind the supplied governance evidence")
        if signature["signing_key"]["key_id"] != self.release_key_id:
            raise GovernedDistributionError("release signature key is not trusted")
        try:
            verify_release_signature(candidate, authorization, signature, self.release_public_key)
        except ReleaseSigningError as exc:
            raise GovernedDistributionError("release signature verification failed") from exc
        return bundle


def prepare_activation_package(
    decrypted_staging: Path | str,
    output: Path | str,
    *,
    governed_verifier: GovernedReleaseVerifier | None = None,
    allow_legacy_lab: bool = False,
) -> Path:
    """Verify governance, safely extract corpus, then install manifest.sig last."""
    source = Path(decrypted_staging)
    record = _json_object(source / "decryption-record.json", "decryption record")
    required_record_fields = {
        "schema_version", "record_type", "source_manifest_digest", "scope",
        "sequence", "contract_profile", "artifacts",
    }
    if set(record) != required_record_fields \
            or record.get("schema_version") != "1.1" \
            or record.get("record_type") != "decrypted_transfer_staging" \
            or not isinstance(record.get("artifacts"), list):
        raise GovernedDistributionError("decryption record contract is invalid")
    profile = record.get("contract_profile")
    if profile not in {GOVERNED_PROFILE, LEGACY_PROFILE}:
        raise GovernedDistributionError("decryption record has no supported contract profile")
    inventory = {item.get("name"): item for item in record["artifacts"] if isinstance(item, dict)}
    if set(inventory) != {"application", "model", "corpus", "signature_bundle", "activation_request"}:
        raise GovernedDistributionError("decryption record artifact inventory is incomplete")
    corpus = source / "artifacts" / "corpus"
    bundle_path = source / "artifacts" / "signature_bundle"
    if candidate_contract_profile(read_candidate_manifest(corpus)) != profile:
        raise GovernedDistributionError("transfer and corpus contract profiles do not match")
    bundle = None
    if profile == GOVERNED_PROFILE:
        if governed_verifier is None:
            raise GovernedDistributionError("governed activation requires a trusted release verifier")
        bundle = governed_verifier.verify(corpus, bundle_path)
    elif not allow_legacy_lab:
        raise GovernedDistributionError("legacy activation requires explicit lab compatibility")

    target = Path(output)
    if target.exists():
        raise FileExistsError(f"activation package already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=str(target.parent)))
    try:
        with tarfile.open(corpus, "r:*") as archive:
            members = _archive_members(archive)
            if profile == GOVERNED_PROFILE and any(member.name == "manifest.sig" for member in members):
                raise GovernedDistributionError("governed corpus archive must not preinstall manifest.sig")
            for member in members:
                destination = staging / member.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                handle = archive.extractfile(member)
                if handle is None:
                    raise GovernedDistributionError("corpus archive member cannot be read")
                with destination.open("wb") as output_handle:
                    shutil.copyfileobj(handle, output_handle)
        if profile == GOVERNED_PROFILE:
            evidence = staging / "governance"
            evidence.mkdir(mode=0o700)
            for name, key in (
                ("release-signature.json", "release_signature"),
                ("release-signing-authorization.json", "release_signing_authorization"),
                ("promotion-report.json", "promotion_report"),
                ("evaluation-attestation.json", "evaluation_attestation"),
            ):
                (evidence / name).write_text(json.dumps(bundle[key], indent=2, sort_keys=True) + "\n", encoding="utf-8")
            (staging / "manifest.sig").write_text(
                bundle["release_signature"]["manifest_signature"]["value"] + "\n",
                encoding="ascii",
            )
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target


def import_governed_encrypted_transfer(
    root: Path | str,
    inbox: Path | str,
    decrypted_output: Path | str,
    *,
    expected_event_id: str,
    expected_site_id: str,
    allowed_classifications: Set[str],
    signature_verifier: Callable[[bytes, bytes], bool],
    recipient_key_id: str,
    private_key: Any,
    governed_verifier: GovernedReleaseVerifier,
) -> tuple[Path, Path]:
    """Verify and decrypt governance before committing inbox replay state."""
    from corpus_factory.distribution import (
        decrypt_transfer_set,
        import_transfer_set,
        verify_transfer_set,
    )

    output = Path(decrypted_output)
    if output.exists():
        raise FileExistsError(f"decryption output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f".{output.name}.verify.", dir=str(output.parent)))
    verified_staging = scratch / "decrypted"
    try:
        verified = verify_transfer_set(
            Path(root),
            expected_event_id=expected_event_id,
            expected_site_id=expected_site_id,
            allowed_classifications=allowed_classifications,
            signature_verifier=signature_verifier,
            governed_release_verifier=governed_verifier,
            require_governed_release=True,
        )
        decrypt_transfer_set(
            verified,
            verified_staging,
            site_id=expected_site_id,
            recipient_key_id=recipient_key_id,
            private_key=private_key,
            governed_release_verifier=governed_verifier,
            require_governed_release=True,
        )
        imported = import_transfer_set(
            Path(root),
            Path(inbox),
            expected_event_id=expected_event_id,
            expected_site_id=expected_site_id,
            allowed_classifications=allowed_classifications,
            signature_verifier=signature_verifier,
            governed_release_verifier=governed_verifier,
            require_governed_release=True,
        )
        os.replace(verified_staging, output)
        return imported, output
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
