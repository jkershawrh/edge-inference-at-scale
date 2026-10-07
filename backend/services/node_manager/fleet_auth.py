"""Offline-verifiable identity and replay protection for fleet messages.

The enrollment registry is controller-owned configuration.  Edge nodes hold
only their Ed25519 private key and monotonically increasing sequence.  Replay
state is kept in SQLite so a controller restart does not reopen the window.
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class FleetAuthError(ValueError):
    """A fleet control-plane message could not be authenticated."""


def canonical_fleet_message(
    *,
    kind: str,
    node_id: str,
    key_id: str,
    issued_at: int,
    sequence: int,
    payload: Mapping[str, Any],
) -> bytes:
    """Return the domain-separated bytes signed by a field node."""
    envelope = {
        "schema_version": 1,
        "kind": kind,
        "node_id": node_id,
        "key_id": key_id,
        "issued_at": issued_at,
        "sequence": sequence,
        "payload": payload,
    }
    return (
        "lil-evy-fleet-control-v1\n"
        + json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    ).encode("utf-8")


class FleetReplayStore:
    """Durable, atomic high-water marks for each enrolled node."""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fleet_replay_state (
                    node_id TEXT PRIMARY KEY,
                    last_sequence INTEGER NOT NULL CHECK(last_sequence >= 1),
                    updated_at INTEGER NOT NULL
                )
                """
            )

    def advance(self, node_id: str, sequence: int, *, now: int) -> None:
        """Atomically require and store a strictly increasing sequence."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT last_sequence FROM fleet_replay_state WHERE node_id = ?",
                (node_id,),
            ).fetchone()
            if row is not None and sequence <= row[0]:
                raise FleetAuthError("fleet message sequence is stale or replayed")
            connection.execute(
                """
                INSERT INTO fleet_replay_state(node_id, last_sequence, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    last_sequence = excluded.last_sequence,
                    updated_at = excluded.updated_at
                """,
                (node_id, sequence, now),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def last_sequence(self, node_id: str) -> Optional[int]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_sequence FROM fleet_replay_state WHERE node_id = ?",
                (node_id,),
            ).fetchone()
        return row[0] if row else None


class FleetAuthenticator:
    """Verify a node identity against an operator-managed enrollment registry."""

    def __init__(
        self,
        registry_path: Union[str, Path],
        replay_store: FleetReplayStore,
        *,
        max_clock_skew_seconds: int = 300,
    ):
        if max_clock_skew_seconds < 1:
            raise ValueError("max_clock_skew_seconds must be positive")
        self.registry_path = Path(registry_path)
        self.replay_store = replay_store
        self.max_clock_skew_seconds = max_clock_skew_seconds

    def _enrollment(self, node_id: str, key_id: str) -> Mapping[str, Any]:
        try:
            registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FleetAuthError("fleet enrollment registry is unavailable or invalid") from exc
        if registry.get("schema_version") != 1 or not isinstance(registry.get("nodes"), dict):
            raise FleetAuthError("fleet enrollment registry has an unsupported schema")
        enrollment = registry["nodes"].get(node_id)
        if not isinstance(enrollment, dict):
            raise FleetAuthError("node is not enrolled")
        if enrollment.get("key_id") != key_id:
            raise FleetAuthError("node key id does not match enrollment")
        if enrollment.get("enabled") is not True:
            raise FleetAuthError("node enrollment is disabled")
        return enrollment

    def verify(
        self,
        *,
        kind: str,
        payload: Mapping[str, Any],
        proof: Mapping[str, Any],
        now: Optional[int] = None,
    ) -> None:
        node_id = payload.get("node_id")
        key_id = proof.get("key_id")
        issued_at = proof.get("issued_at")
        sequence = proof.get("sequence")
        signature_text = proof.get("signature")
        if not isinstance(node_id, str) or not node_id:
            raise FleetAuthError("fleet payload has no node identity")
        if not isinstance(key_id, str) or not key_id:
            raise FleetAuthError("fleet proof has no key id")
        if not isinstance(issued_at, int) or isinstance(issued_at, bool):
            raise FleetAuthError("fleet proof has an invalid timestamp")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise FleetAuthError("fleet proof has an invalid sequence")
        if not isinstance(signature_text, str):
            raise FleetAuthError("fleet proof has no signature")

        checked_at = int(time.time()) if now is None else now
        if abs(checked_at - issued_at) > self.max_clock_skew_seconds:
            raise FleetAuthError("fleet message timestamp is outside the allowed window")
        enrollment = self._enrollment(node_id, key_id)
        try:
            public_key = serialization.load_pem_public_key(
                enrollment["public_key_pem"].encode("ascii")
            )
        except (KeyError, TypeError, ValueError, UnicodeEncodeError) as exc:
            raise FleetAuthError("enrolled public key is invalid") from exc
        if not isinstance(public_key, Ed25519PublicKey):
            raise FleetAuthError("enrolled key must be Ed25519")
        try:
            signature = base64.b64decode(signature_text, validate=True)
            public_key.verify(
                signature,
                canonical_fleet_message(
                    kind=kind,
                    node_id=node_id,
                    key_id=key_id,
                    issued_at=issued_at,
                    sequence=sequence,
                    payload=payload,
                ),
            )
        except (InvalidSignature, binascii.Error, ValueError) as exc:
            raise FleetAuthError("fleet message signature is invalid") from exc
        self.replay_store.advance(node_id, sequence, now=checked_at)
