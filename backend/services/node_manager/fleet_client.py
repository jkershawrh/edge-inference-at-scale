"""Node-side construction of authenticated fleet control messages."""

import base64
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .fleet_auth import canonical_fleet_message


class FleetSequenceStore:
    """Atomically reserve monotonically increasing node message sequences."""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS node_sequence (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), value INTEGER NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def reserve_next(self) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM node_sequence WHERE singleton = 1"
            ).fetchone()
            value = 1 if row is None else row[0] + 1
            connection.execute(
                "INSERT OR REPLACE INTO node_sequence(singleton, value) VALUES (1, ?)",
                (value,),
            )
            connection.commit()
            return value
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class FleetMessageSigner:
    """Sign normalized registration or heartbeat payloads with a node key."""

    def __init__(
        self,
        *,
        node_id: str,
        key_id: str,
        private_key_path: Union[str, Path],
        sequence_store: FleetSequenceStore,
    ):
        if not node_id or not key_id:
            raise ValueError("node_id and key_id are required")
        self.node_id = node_id
        self.key_id = key_id
        self.private_key_path = Path(private_key_path)
        self.sequence_store = sequence_store

    def _private_key(self) -> Ed25519PrivateKey:
        try:
            key = serialization.load_pem_private_key(
                self.private_key_path.read_bytes(), password=None
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("fleet node private key is unavailable or invalid") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("fleet node private key must be Ed25519")
        return key

    def sign(
        self,
        *,
        kind: str,
        payload: Mapping[str, Any],
        issued_at: Optional[int] = None,
    ) -> Dict[str, Any]:
        if kind not in {"registration", "heartbeat"}:
            raise ValueError("unsupported fleet message kind")
        if payload.get("node_id") != self.node_id:
            raise ValueError("payload node_id does not match signer identity")
        timestamp = int(time.time()) if issued_at is None else issued_at
        sequence = self.sequence_store.reserve_next()
        signature = self._private_key().sign(
            canonical_fleet_message(
                kind=kind,
                node_id=self.node_id,
                key_id=self.key_id,
                issued_at=timestamp,
                sequence=sequence,
                payload=payload,
            )
        )
        return {
            "key_id": self.key_id,
            "issued_at": timestamp,
            "sequence": sequence,
            "signature": base64.b64encode(signature).decode("ascii"),
        }
