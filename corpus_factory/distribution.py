"""Deterministic, offline transfer sets for disconnected Lil EVY sites."""
from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Set


SCHEMA_VERSION = "1.0"
ENCRYPTED_SCHEMA_VERSION = "2.0"
MANIFEST_MEDIA_TYPE = "application/vnd.evy.transfer.manifest.v1+json"
ENCRYPTED_MANIFEST_MEDIA_TYPE = "application/vnd.evy.transfer.manifest.v2+json"
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


@dataclass(frozen=True)
class EncryptionRecipient:
    """A site-scoped X25519 public key used only for content-key wrapping."""

    site_id: str
    key_id: str
    public_key: Any


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: Any, label: str) -> bytes:
    if not isinstance(value, str):
        raise DistributionError("invalid %s" % label)
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise DistributionError("invalid %s" % label) from exc


def _crypto():
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PrivateKey, X25519PublicKey,
        )
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
        from cryptography.hazmat.primitives.hmac import HMAC
    except ImportError as exc:
        raise DistributionError("cryptography is required for encrypted transfers") from exc
    return hashes, serialization, X25519PrivateKey, X25519PublicKey, AESGCM, HKDFExpand, HMAC


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    hashes, _serialization, _priv, _pub, _aes, _expand, HMAC = _crypto()
    mac = HMAC(salt or (b"\x00" * 32), hashes.SHA256())
    mac.update(ikm)
    return mac.finalize()


def _labeled_extract(suite_id: bytes, salt: bytes, label: bytes, ikm: bytes) -> bytes:
    return _hkdf_extract(salt, b"HPKE-v1" + suite_id + label + ikm)


def _labeled_expand(suite_id: bytes, prk: bytes, label: bytes, info: bytes, length: int) -> bytes:
    hashes, _serialization, _priv, _pub, _aes, HKDFExpand, _hmac = _crypto()
    labeled = length.to_bytes(2, "big") + b"HPKE-v1" + suite_id + label + info
    return HKDFExpand(algorithm=hashes.SHA256(), length=length, info=labeled).derive(prk)


def _hpke_context(shared_secret: bytes, info: bytes) -> tuple[bytes, bytes]:
    # RFC 9180 base mode: DHKEM(X25519, HKDF-SHA256), HKDF-SHA256, AES-256-GCM.
    suite = b"HPKE" + b"\x00\x20" + b"\x00\x01" + b"\x00\x02"
    psk_id_hash = _labeled_extract(suite, b"", b"psk_id_hash", b"")
    info_hash = _labeled_extract(suite, b"", b"info_hash", info)
    context = b"\x00" + psk_id_hash + info_hash
    secret = _labeled_extract(suite, shared_secret, b"secret", b"")
    return (
        _labeled_expand(suite, secret, b"key", context, 32),
        _labeled_expand(suite, secret, b"base_nonce", context, 12),
    )


def _hpke_wrap(public_key: Any, content_key: bytes, info: bytes, aad: bytes) -> tuple[bytes, bytes]:
    _hashes, serialization, X25519PrivateKey, X25519PublicKey, AESGCM, _expand, _hmac = _crypto()
    if not isinstance(public_key, X25519PublicKey):
        raise DistributionError("recipient public key must be X25519")
    ephemeral = X25519PrivateKey.generate()
    enc = ephemeral.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    recipient_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    kem_suite = b"KEM" + b"\x00\x20"
    eae_prk = _labeled_extract(kem_suite, b"", b"eae_prk", ephemeral.exchange(public_key))
    shared = _labeled_expand(kem_suite, eae_prk, b"shared_secret", enc + recipient_bytes, 32)
    key, nonce = _hpke_context(shared, info)
    return enc, AESGCM(key).encrypt(nonce, content_key, aad)


def _hpke_unwrap(private_key: Any, enc: bytes, wrapped: bytes, info: bytes, aad: bytes) -> bytes:
    _hashes, serialization, X25519PrivateKey, X25519PublicKey, AESGCM, _expand, _hmac = _crypto()
    if not isinstance(private_key, X25519PrivateKey):
        raise DistributionError("recipient private key must be X25519")
    if len(enc) != 32:
        raise DistributionError("invalid HPKE encapsulated key")
    ephemeral = X25519PublicKey.from_public_bytes(enc)
    recipient_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    kem_suite = b"KEM" + b"\x00\x20"
    eae_prk = _labeled_extract(kem_suite, b"", b"eae_prk", private_key.exchange(ephemeral))
    shared = _labeled_expand(kem_suite, eae_prk, b"shared_secret", enc + recipient_bytes, 32)
    key, nonce = _hpke_context(shared, info)
    try:
        return AESGCM(key).decrypt(nonce, wrapped, aad)
    except Exception as exc:
        raise DistributionError("content key unwrap failed") from exc


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
    allow_unencrypted_lab: bool = False,
) -> Path:
    """Create a plaintext transfer set only under an explicit public/lab policy."""
    if classification != "public" and not allow_unencrypted_lab:
        raise DistributionError(
            "unencrypted non-public transfer requires explicit lab compatibility"
        )
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


def export_encrypted_transfer_set(
    output: Path, artifacts: Mapping[str, Path], *, media_id: str, event_id: str,
    site_id: str, sequence: int, classification: str,
    recipient: EncryptionRecipient,
    manifest_signer: Optional[ManifestSigner] = None,
) -> Path:
    """Create a signed-ready, site-bound AES-256-GCM/HPKE transfer set.

    The signing callback remains independent: it receives only the completed
    ciphertext manifest and never receives a content or recipient private key.
    """
    if classification == "public":
        raise DistributionError("public transfers must use the plaintext carrier")
    _validate_id("media ID", media_id)
    _validate_id("event ID", event_id)
    _validate_id("site ID", site_id)
    _validate_id("classification", classification)
    _validate_id("recipient site ID", recipient.site_id)
    _validate_id("recipient key ID", recipient.key_id)
    if recipient.site_id != site_id:
        raise DistributionError("encryption recipient does not match target site")
    if type(sequence) is not int or sequence < 1:
        raise DistributionError("sequence must be a positive integer")
    if set(artifacts) != set(REQUIRED_ARTIFACTS):
        raise DistributionError("transfer set must declare exactly the required artifacts")
    output = Path(output)
    if output.exists():
        raise FileExistsError("immutable transfer set already exists: %s" % output)
    output.parent.mkdir(parents=True, exist_ok=True)
    _hashes, _serialization, _priv, _pub, AESGCM, _expand, _hmac = _crypto()
    content_key = os.urandom(32)
    identity = {
        "media_id": media_id,
        "event_id": event_id,
        "site_id": site_id,
        "sequence": sequence,
        "classification": classification,
    }
    wrap_aad = _canonical({**identity, "recipient_key_id": recipient.key_id})
    hpke_info = b"evy-transfer-content-key-v1"
    enc, wrapped_key = _hpke_wrap(
        recipient.public_key, content_key, hpke_info, wrap_aad
    )
    staging = Path(tempfile.mkdtemp(prefix=".%s." % output.name, dir=str(output.parent)))
    try:
        blob_dir = staging / "blobs" / "sha256"
        blob_dir.mkdir(parents=True)
        records = []
        for ordinal, name in enumerate(REQUIRED_ARTIFACTS):
            source = Path(artifacts[name])
            if source.is_symlink() or not source.is_file():
                raise DistributionError("artifact must be a regular file: %s" % name)
            plaintext = source.read_bytes()
            plaintext_digest = "sha256:" + hashlib.sha256(plaintext).hexdigest()
            nonce = os.urandom(12)
            aad_record = {
                **identity,
                "artifact": name,
                "media_type": ARTIFACT_MEDIA_TYPES[name],
                "ordinal": ordinal,
                "plaintext_digest": plaintext_digest,
            }
            ciphertext = AESGCM(content_key).encrypt(nonce, plaintext, _canonical(aad_record))
            cipher_hash = hashlib.sha256(ciphertext).hexdigest()
            (blob_dir / cipher_hash).write_bytes(ciphertext)
            records.append({
                "name": name,
                "media_type": ARTIFACT_MEDIA_TYPES[name],
                "digest": "sha256:" + cipher_hash,
                "size": len(ciphertext),
                "path": "blobs/sha256/" + cipher_hash,
                "encryption": {
                    "algorithm": "AES-256-GCM",
                    "nonce": _b64(nonce),
                    "plaintext_digest": plaintext_digest,
                    "plaintext_size": len(plaintext),
                    "aad": aad_record,
                },
            })
        manifest = {
            "schema_version": ENCRYPTED_SCHEMA_VERSION,
            "media_type": ENCRYPTED_MANIFEST_MEDIA_TYPE,
            "media_id": media_id,
            "scope": {"event_id": event_id, "site_id": site_id},
            "sequence": sequence,
            "classification": classification,
            "envelope": {
                "algorithm": "HPKE-v1-BASE-X25519-HKDF-SHA256-AES-256-GCM",
                "info": _b64(hpke_info),
                "recipient": {
                    "site_id": recipient.site_id,
                    "key_id": recipient.key_id,
                    "encapsulated_key": _b64(enc),
                    "wrapped_key": _b64(wrapped_key),
                    "aad": json.loads(wrap_aad),
                },
            },
            "artifacts": records,
        }
        manifest_bytes = _canonical(manifest)
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        (blob_dir / manifest_hash).write_bytes(manifest_bytes)
        index = {"schemaVersion": 2, "manifests": [{
            "mediaType": ENCRYPTED_MANIFEST_MEDIA_TYPE,
            "digest": "sha256:" + manifest_hash,
            "size": len(manifest_bytes),
        }]}
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
    finally:
        # Python byte strings cannot be reliably zeroized; keep the key scoped
        # to this function and never persist or log it.
        del content_key
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
            or descriptor.get("mediaType") not in {
                MANIFEST_MEDIA_TYPE, ENCRYPTED_MANIFEST_MEDIA_TYPE
            }:
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
    if descriptor["mediaType"] != manifest["media_type"]:
        raise DistributionError("transfer manifest media type mismatch")
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
    encrypted = isinstance(manifest, dict) and manifest.get("schema_version") == ENCRYPTED_SCHEMA_VERSION
    required = {"schema_version", "media_type", "media_id", "scope", "sequence", "classification", "artifacts"}
    if encrypted:
        required.add("envelope")
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise DistributionError("invalid transfer manifest fields")
    expected_media_type = ENCRYPTED_MANIFEST_MEDIA_TYPE if encrypted else MANIFEST_MEDIA_TYPE
    if manifest["schema_version"] not in {SCHEMA_VERSION, ENCRYPTED_SCHEMA_VERSION} \
            or manifest["media_type"] != expected_media_type:
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
    if encrypted:
        _validate_envelope(manifest["envelope"], manifest)
        fields.add("encryption")
    nonces = []
    for ordinal, item in enumerate(artifacts):
        if set(item) != fields or item["media_type"] != ARTIFACT_MEDIA_TYPES[item["name"]] \
                or not _DIGEST.fullmatch(str(item["digest"])) \
                or type(item["size"]) is not int or item["size"] < 0 \
                or not isinstance(item["path"], str):
            raise DistributionError("invalid transfer artifact descriptor")
        if encrypted:
            _validate_layer_encryption(item, manifest, ordinal)
            nonces.append(item["encryption"]["nonce"])
    if encrypted and len(nonces) != len(set(nonces)):
        raise DistributionError("AES-GCM nonce reuse is forbidden")


def _validate_envelope(envelope: Any, manifest: Mapping[str, Any]) -> None:
    if not isinstance(envelope, dict) or set(envelope) != {"algorithm", "info", "recipient"} \
            or envelope.get("algorithm") != "HPKE-v1-BASE-X25519-HKDF-SHA256-AES-256-GCM":
        raise DistributionError("invalid encryption envelope")
    if _unb64(envelope.get("info"), "HPKE info") != b"evy-transfer-content-key-v1":
        raise DistributionError("invalid HPKE info")
    recipient = envelope.get("recipient")
    fields = {"site_id", "key_id", "encapsulated_key", "wrapped_key", "aad"}
    if not isinstance(recipient, dict) or set(recipient) != fields:
        raise DistributionError("invalid encryption recipient")
    _validate_id("recipient site ID", recipient.get("site_id"))
    _validate_id("recipient key ID", recipient.get("key_id"))
    if recipient["site_id"] != manifest["scope"]["site_id"]:
        raise DistributionError("encryption recipient does not match target site")
    if len(_unb64(recipient["encapsulated_key"], "encapsulated key")) != 32 \
            or len(_unb64(recipient["wrapped_key"], "wrapped key")) != 48:
        raise DistributionError("invalid wrapped content key")
    expected_aad = {
        "media_id": manifest["media_id"],
        "event_id": manifest["scope"]["event_id"],
        "site_id": manifest["scope"]["site_id"],
        "sequence": manifest["sequence"],
        "classification": manifest["classification"],
        "recipient_key_id": recipient["key_id"],
    }
    if recipient["aad"] != expected_aad:
        raise DistributionError("encryption recipient metadata is not bound to transfer scope")


def _validate_layer_encryption(
    item: Mapping[str, Any], manifest: Mapping[str, Any], ordinal: int
) -> None:
    encryption = item.get("encryption")
    fields = {"algorithm", "nonce", "plaintext_digest", "plaintext_size", "aad"}
    if not isinstance(encryption, dict) or set(encryption) != fields \
            or encryption.get("algorithm") != "AES-256-GCM" \
            or len(_unb64(encryption.get("nonce"), "AES-GCM nonce")) != 12 \
            or not _DIGEST.fullmatch(str(encryption.get("plaintext_digest", ""))) \
            or type(encryption.get("plaintext_size")) is not int \
            or encryption["plaintext_size"] < 0:
        raise DistributionError("invalid layer encryption metadata")
    expected_aad = {
        "media_id": manifest["media_id"],
        "event_id": manifest["scope"]["event_id"],
        "site_id": manifest["scope"]["site_id"],
        "sequence": manifest["sequence"],
        "classification": manifest["classification"],
        "artifact": item["name"],
        "media_type": item["media_type"],
        "ordinal": ordinal,
        "plaintext_digest": encryption["plaintext_digest"],
    }
    if encryption["aad"] != expected_aad:
        raise DistributionError("layer encryption metadata is not bound to transfer identity")


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


def decrypt_transfer_set(
    verified: VerifiedTransferSet, output: Path, *, site_id: str,
    recipient_key_id: str, private_key: Any,
) -> Path:
    """Decrypt a previously verified v2 transfer into an atomic staging directory."""
    manifest = verified.manifest
    if manifest.get("schema_version") != ENCRYPTED_SCHEMA_VERSION:
        raise DistributionError("transfer set is not encrypted")
    _validate_manifest(manifest)
    _validate_id("site ID", site_id)
    _validate_id("recipient key ID", recipient_key_id)
    if manifest["scope"]["site_id"] != site_id:
        raise DistributionError("encrypted transfer is scoped to a different site")
    recipient = manifest["envelope"]["recipient"]
    if recipient["site_id"] != site_id or recipient["key_id"] != recipient_key_id:
        raise DistributionError("no encryption recipient matches this site and key")
    output = Path(output)
    if output.exists():
        raise FileExistsError("decryption output already exists: %s" % output)
    wrap_aad = _canonical(recipient["aad"])
    content_key = _hpke_unwrap(
        private_key,
        _unb64(recipient["encapsulated_key"], "encapsulated key"),
        _unb64(recipient["wrapped_key"], "wrapped key"),
        _unb64(manifest["envelope"]["info"], "HPKE info"),
        wrap_aad,
    )
    _hashes, _serialization, _priv, _pub, AESGCM, _expand, _hmac = _crypto()
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".%s." % output.name, dir=str(output.parent)))
    try:
        artifact_dir = staging / "artifacts"
        artifact_dir.mkdir(mode=0o700)
        inventory = []
        for item in manifest["artifacts"]:
            encrypted = (verified.root / item["path"]).read_bytes()
            metadata = item["encryption"]
            try:
                plaintext = AESGCM(content_key).decrypt(
                    _unb64(metadata["nonce"], "AES-GCM nonce"),
                    encrypted,
                    _canonical(metadata["aad"]),
                )
            except Exception as exc:
                raise DistributionError("artifact decryption failed: %s" % item["name"]) from exc
            digest = "sha256:" + hashlib.sha256(plaintext).hexdigest()
            if digest != metadata["plaintext_digest"] or len(plaintext) != metadata["plaintext_size"]:
                raise DistributionError("plaintext integrity check failed: %s" % item["name"])
            target = artifact_dir / item["name"]
            target.write_bytes(plaintext)
            target.chmod(0o600)
            inventory.append({
                "name": item["name"],
                "media_type": item["media_type"],
                "digest": digest,
                "size": len(plaintext),
                "path": "artifacts/" + item["name"],
            })
        record = {
            "schema_version": "1.0",
            "record_type": "decrypted_transfer_staging",
            "source_manifest_digest": verified.manifest_digest,
            "scope": manifest["scope"],
            "sequence": manifest["sequence"],
            "artifacts": inventory,
        }
        record_path = staging / "decryption-record.json"
        record_path.write_bytes(_canonical(record))
        record_path.chmod(0o600)
        os.replace(str(staging), str(output))
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        del content_key
    return output


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
