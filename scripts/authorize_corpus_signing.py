#!/usr/bin/env python3
"""Authorize external release signing for one evaluated corpus candidate digest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from corpus_factory.evaluation_attestation import (
    EvaluationAttestationError,
    authorize_release_signing,
    load_ed25519_public_key,
    load_json_object,
)


def _optional_list(path: str | None) -> list[str]:
    if path is None:
        return []
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise EvaluationAttestationError("revocation input must be a JSON string array")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-manifest", required=True)
    parser.add_argument("--promotion-report", required=True)
    parser.add_argument("--evaluation-attestation", required=True)
    parser.add_argument("--attestation-public-key", required=True)
    parser.add_argument("--as-of", required=True, help="trusted ISO-8601 time")
    parser.add_argument("--revoked-attestations")
    parser.add_argument("--revoked-keys")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        authorization = authorize_release_signing(
            Path(args.candidate_manifest).read_bytes(),
            load_json_object(args.promotion_report),
            load_json_object(args.evaluation_attestation),
            load_ed25519_public_key(Path(args.attestation_public_key).read_bytes()),
            as_of=args.as_of,
            revoked_attestation_ids=_optional_list(args.revoked_attestations),
            revoked_key_ids=_optional_list(args.revoked_keys),
        )
        Path(args.output).write_text(
            json.dumps(authorization, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (OSError, UnicodeError, json.JSONDecodeError, EvaluationAttestationError) as exc:
        print(f"Release signing authorization failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
