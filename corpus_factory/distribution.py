"""Deterministic, offline transfer sets for disconnected Lil EVY sites."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Set


SCHEMA_VERSION = "1.0"
MANIFEST_MEDIA_TYPE = "application/vnd.evy.transfer.manifest.v1+json"
REQUIRED_ARTIFACTS = (
    "application", "model", "corpus", "signature_bundle", "activation_request"
)
ARTIFACT_MEDIA_TYPES = {
    "application": "application/vnd.oci.image.manifest.v1+json",
    "model": "application/vnd.evy.model.v1",
    "corpus": "application/vnd.evy.corpus.v2",
    "signature_bundle": "application/vnd.evy.signatures.v1+json",
    "activation_request": "application/vnd.evy.activation-request.v1+json",
}
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
_DIGEST = re.compile(r"^sha256:([a-f0-9]{64})$")


class DistributionError(ValueError):
    """A transfer set is incomplete, untrusted, replayed, or out of scope."""


ManifestSigner = Callable[[bytes], bytes]
ManifestSignatureVerifier = Callable[[bytes, bytes], bool]


@dataclass(frozen=True)
class VerifiedTransferSet:
    root: Path
    manifest: Mapping[str, Any]
    manifest_digest: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _decode_json(text: str, label: str) -> Dict[str, Any]:
    def no_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise DistributionError("duplicate JSON key: %s" % key)
            value[key] = item
        return value

    try:
        result = json.loads(text, object_pairs_hook=no_duplicates)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DistributionError("invalid JSON: %s" % label) from exc
    if not isinstance(result, dict):
        raise DistributionError("JSON record must be an object: %s" % label)
    return result


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DistributionError("invalid JSON: %s" % path.name) from exc
    return _decode_json(text, path.name)


def _validate_id(label: str, value: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise DistributionError("invalid %s" % label)


def build_transfer_manifest(
    artifacts: Mapping[str, Path], *, media_id: str, event_id: str, site_id: str,
    sequence: int, classification: str,
) -> Dict[str, Any]:
    """Build a deterministic manifest for the five required offline artifacts."""
    _validate_id("media ID", media_id)
    _validate_id("event ID", event_id)
    _validate_id("site ID", site_id)
    _validate_id("classification", classification)
    if type(sequence) is not int or sequence < 1:
        raise DistributionError("sequence must be a positive integer")
    if set(artifacts) != set(REQUIRED_ARTIFACTS):
        raise DistributionError("transfer set must declare exactly the required artifacts")
    records = []
    for name in REQUIRED_ARTIFACTS:
        source = Path(artifacts[name])
        if source.is_symlink() or not source.is_file():
            raise DistributionError("artifact must be a regular file: %s" % name)
        hexdigest = _sha256(source)
        records.append({
            "name": name,
            "media_type": ARTIFACT_MEDIA_TYPES[name],
            "digest": "sha256:" + hexdigest,
            "size": source.stat().st_size,
            "path": "blobs/sha256/" + hexdigest,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "media_type": MANIFEST_MEDIA_TYPE,
        "media_id": media_id,
        "scope": {"event_id": event_id, "site_id": site_id},
        "sequence": sequence,
        "classification": classification,
        "artifacts": records,
    }


def export_transfer_set(
    output: Path, artifacts: Mapping[str, Path], *, media_id: str, event_id: str,
    site_id: str, sequence: int, classification: str,
    manifest_signer: Optional[ManifestSigner] = None,
) -> Path:
    """Atomically create an OCI-layout-style directory containing declared blobs only."""
    output = Path(output)
    if output.exists():
        raise FileExistsError("immutable transfer set already exists: %s" % output)
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = build_transfer_manifest(
        artifacts, media_id=media_id, event_id=event_id, site_id=site_id,
        sequence=sequence, classification=classification,
    )
    staging = Path(tempfile.mkdtemp(prefix=".%s." % output.name, dir=str(output.parent)))
    try:
        blob_dir = staging / "blobs" / "sha256"
        blob_dir.mkdir(parents=True)
        for record in manifest["artifacts"]:
            copied = staging / record["path"]
            shutil.copyfile(str(artifacts[record["name"]]), str(copied))
            if copied.stat().st_size != record["size"] or _sha256(copied) != record["digest"].split(":", 1)[1]:
                raise DistributionError("artifact changed while being exported: %s" % record["name"])
        manifest_bytes = _canonical(manifest)
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        (blob_dir / manifest_hash).write_bytes(manifest_bytes)
        index = {
            "schemaVersion": 2,
            "manifests": [{
                "mediaType": MANIFEST_MEDIA_TYPE,
                "digest": "sha256:" + manifest_hash,
                "size": len(manifest_bytes),
            }],
        }
        (staging / "oci-layout").write_bytes(_canonical({"imageLayoutVersion": "1.0.0"}))
        (staging / "index.json").write_bytes(_canonical(index))
        if manifest_signer:
            signature = manifest_signer(manifest_bytes)
            if not isinstance(signature, bytes) or not signature:
                raise DistributionError("manifest signer must return non-empty bytes")
            (staging / "transfer-manifest.sig").write_bytes(signature)
        os.replace(str(staging), str(output))
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return output


def verify_transfer_set(
    root: Path, *, expected_event_id: str, expected_site_id: str,
    allowed_classifications: Set[str], sequence_floor: int = 0,
    used_media_ids: Sequence[str] = (),
    signature_verifier: Optional[ManifestSignatureVerifier] = None,
    require_signature: bool = True,
) -> VerifiedTransferSet:
    """Verify closed-world contents, integrity, scope, replay, and optional signature."""
    root = Path(root)
    _validate_id("expected event ID", expected_event_id)
    _validate_id("expected site ID", expected_site_id)
    if root.is_symlink() or not root.is_dir():
        raise DistributionError("transfer set must be a regular directory")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise DistributionError("transfer set cannot contain symbolic links")
    layout = _load_json(root / "oci-layout")
    if layout != {"imageLayoutVersion": "1.0.0"}:
        raise DistributionError("unsupported OCI layout")
    index = _load_json(root / "index.json")
    descriptors = index.get("manifests")
    if set(index) != {"schemaVersion", "manifests"} or index.get("schemaVersion") != 2 \
            or not isinstance(descriptors, list) or len(descriptors) != 1:
        raise DistributionError("invalid transfer index")
    descriptor = descriptors[0]
    if not isinstance(descriptor, dict) or set(descriptor) != {"mediaType", "digest", "size"} \
            or descriptor.get("mediaType") != MANIFEST_MEDIA_TYPE:
        raise DistributionError("invalid transfer manifest descriptor")
    match = _DIGEST.fullmatch(str(descriptor.get("digest", "")))
    if not match or type(descriptor.get("size")) is not int or descriptor["size"] < 0:
        raise DistributionError("invalid transfer manifest digest or size")
    manifest_path = root / "blobs" / "sha256" / match.group(1)
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise DistributionError("transfer manifest blob is missing")
    manifest_bytes = manifest_path.read_bytes()
    if len(manifest_bytes) != descriptor["size"] or hashlib.sha256(manifest_bytes).hexdigest() != match.group(1):
        raise DistributionError("transfer manifest integrity check failed")
    try:
        manifest_text = manifest_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise DistributionError("transfer manifest is invalid JSON") from exc
    manifest = _decode_json(manifest_text, "transfer manifest")
    _validate_manifest(manifest)
    if _canonical(manifest) != manifest_bytes:
        raise DistributionError("transfer manifest is not canonical")
    if manifest["scope"] != {"event_id": expected_event_id, "site_id": expected_site_id}:
        raise DistributionError("transfer scope does not match target site and event")
    if manifest["classification"] not in allowed_classifications:
        raise DistributionError("transfer classification is not allowed")
    if manifest["sequence"] <= sequence_floor:
        raise DistributionError("transfer sequence does not advance the site floor")
    if manifest["media_id"] in set(used_media_ids):
        raise DistributionError("transfer media ID has already been used")
    expected_files = {"oci-layout", "index.json"}
    expected_blobs = {match.group(1)}
    for record in manifest["artifacts"]:
        blob_match = _DIGEST.fullmatch(record["digest"])
        hexdigest = blob_match.group(1)
        if record["path"] != "blobs/sha256/" + hexdigest:
            raise DistributionError("artifact path is not content-addressed")
        path = root / record["path"]
        if not path.is_file() or path.is_symlink():
            raise DistributionError("declared artifact is missing: %s" % record["name"])
        if path.stat().st_size != record["size"] or _sha256(path) != hexdigest:
            raise DistributionError("artifact integrity check failed: %s" % record["name"])
        expected_blobs.add(hexdigest)
    signature_path = root / "transfer-manifest.sig"
    if require_signature and signature_verifier is None:
        raise DistributionError("a trusted transfer signature verifier is required")
    if signature_verifier:
        if not signature_path.is_file() or signature_path.is_symlink():
            raise DistributionError("signed transfer manifest is required")
        try:
            valid_signature = signature_verifier(manifest_bytes, signature_path.read_bytes())
        except Exception as exc:
            raise DistributionError("transfer manifest signature verification failed") from exc
        if valid_signature is not True:
            raise DistributionError("transfer manifest signature verification failed")
    if signature_path.exists():
        expected_files.add("transfer-manifest.sig")
    actual_files = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
    expected_files.update("blobs/sha256/" + digest for digest in expected_blobs)
    if actual_files != expected_files:
        raise DistributionError("transfer set contains extra or undeclared files")
    return VerifiedTransferSet(root.resolve(), manifest, descriptor["digest"])


def _validate_manifest(manifest: Any) -> None:
    required = {"schema_version", "media_type", "media_id", "scope", "sequence", "classification", "artifacts"}
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise DistributionError("invalid transfer manifest fields")
    if manifest["schema_version"] != SCHEMA_VERSION or manifest["media_type"] != MANIFEST_MEDIA_TYPE:
        raise DistributionError("unsupported transfer manifest")
    _validate_id("media ID", manifest["media_id"])
    _validate_id("classification", manifest["classification"])
    if not isinstance(manifest["scope"], dict) or set(manifest["scope"]) != {"event_id", "site_id"}:
        raise DistributionError("invalid transfer scope")
    _validate_id("event ID", manifest["scope"]["event_id"])
    _validate_id("site ID", manifest["scope"]["site_id"])
    if type(manifest["sequence"]) is not int or manifest["sequence"] < 1:
        raise DistributionError("invalid transfer sequence")
    artifacts = manifest["artifacts"]
    if (not isinstance(artifacts, list)
            or len(artifacts) != len(REQUIRED_ARTIFACTS)
            or not all(isinstance(item, dict) for item in artifacts)
            or [item["name"] if "name" in item else None for item in artifacts]
            != list(REQUIRED_ARTIFACTS)):
        raise DistributionError("transfer artifacts are missing, extra, duplicated, or unordered")
    fields = {"name", "media_type", "digest", "size", "path"}
    for item in artifacts:
        if set(item) != fields or item["media_type"] != ARTIFACT_MEDIA_TYPES[item["name"]] \
                or not _DIGEST.fullmatch(str(item["digest"])) \
                or type(item["size"]) is not int or item["size"] < 0 \
                or not isinstance(item["path"], str):
            raise DistributionError("invalid transfer artifact descriptor")


def import_transfer_set(
    root: Path, inbox: Path, *, expected_event_id: str, expected_site_id: str,
    allowed_classifications: Set[str],
    signature_verifier: Optional[ManifestSignatureVerifier] = None,
    require_signature: bool = True,
) -> Path:
    """Verify then atomically copy a transfer set into a site content-addressed inbox."""
    _validate_id("expected event ID", expected_event_id)
    _validate_id("expected site ID", expected_site_id)
    site_root = Path(inbox) / expected_site_id
    state_path = site_root / "import-state.json"
    if state_path.exists():
        state = _load_json(state_path)
    else:
        state = {"sequence_floor": 0, "used_media_ids": []}
    if set(state) != {"sequence_floor", "used_media_ids"} or type(state["sequence_floor"]) is not int \
            or not isinstance(state["used_media_ids"], list):
        raise DistributionError("site import state is invalid")
    verified = verify_transfer_set(
        root, expected_event_id=expected_event_id, expected_site_id=expected_site_id,
        allowed_classifications=allowed_classifications,
        sequence_floor=state["sequence_floor"], used_media_ids=state["used_media_ids"],
        signature_verifier=signature_verifier,
        require_signature=require_signature,
    )
    target = site_root / "transfers" / verified.manifest_digest.split(":", 1)[1]
    if target.exists():
        stored = verify_transfer_set(
            target,
            expected_event_id=expected_event_id,
            expected_site_id=expected_site_id,
            allowed_classifications=allowed_classifications,
            sequence_floor=state["sequence_floor"],
            used_media_ids=state["used_media_ids"],
            signature_verifier=signature_verifier,
            require_signature=require_signature,
        )
        if stored.manifest_digest != verified.manifest_digest:
            raise DistributionError("stored transfer does not match verified media")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".%s." % target.name, dir=str(target.parent)))
        try:
            shutil.copytree(str(verified.root), str(staging / "set"), dirs_exist_ok=True)
            os.replace(str(staging / "set"), str(target))
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    new_state = {"sequence_floor": verified.manifest["sequence"],
                 "used_media_ids": list(state["used_media_ids"]) + [verified.manifest["media_id"]]}
    _atomic_json(state_path, new_state)
    return target


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical(value)); handle.flush(); os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
