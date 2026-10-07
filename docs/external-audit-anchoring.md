# Protected external audit anchoring

The connected Corpus Factory already records acquisition and release actions in
a locked, fsync'd canonical JSONL hash chain. A local hash chain detects edits
inside the files that remain, but an operator with storage access could still
truncate the newest records and present an older valid head. External audit
anchoring closes that gap without giving the factory a witness private key.

## Trust flow

```text
verified local ledger head
  → deterministic checkpoint and predecessor link
  → exact payload sent to an independent witness
  → detached Ed25519 receipt
  → offline verification against the current ledger and trusted witness keys
```

Each checkpoint binds:

- a stable ledger ID;
- a monotonic checkpoint sequence;
- the exact anchored audit-entry sequence and digest;
- the byte length and SHA-256 digest of the complete ledger prefix;
- the preceding receipt ID and full receipt digest;
- the requesting service identity; and
- the checkpoint creation time.

Each witness receipt binds the entire checkpoint plus a separate witness
identity and organization, DER-derived public-key ID, the `audit-witness` key
role, Ed25519 algorithm, key-validity window, revocation generation and check
time, and detached signature. The requester and witness identities must differ.

Offline chain verification rejects altered ledger bytes, missing anchored
entries, rollback below an anchored head, noncontiguous checkpoint sequences,
duplicate receipt replay, two receipts claiming the same sequence, broken
predecessor links, ledger-identity drift, a head that does not advance, backward
witness time, revocation-generation rollback, untrusted keys, and signed or
externally supplied key revocation.

## CLI workflow

Prepare the checkpoint and exact bytes to send to the witness:

```bash
python3 scripts/corpus_audit_anchor.py prepare \
  --ledger state/acquisition-audit.jsonl \
  --ledger-id corpus-factory-production-audit \
  --requester-identity urn:example:service:corpus-factory \
  --created-at 2026-10-06T12:02:00Z \
  --witness-identity urn:example:service:external-audit-witness \
  --witness-organization "Independent Audit Witness" \
  --signed-at 2026-10-06T12:03:00Z \
  --witness-public-key witness-public.pem \
  --key-valid-from 2026-10-01T00:00:00Z \
  --key-expires-at 2027-01-01T00:00:00Z \
  --revocation-generation 4 \
  --revocation-checked-at 2026-10-06T12:02:00Z \
  --output checkpoint-statement.json \
  --payload-output checkpoint.payload
```

The external system signs `checkpoint.payload`. Finalize and verify its result:

```bash
python3 scripts/corpus_audit_anchor.py finalize \
  --statement checkpoint-statement.json \
  --signature checkpoint.signature \
  --witness-public-key witness-public.pem \
  --output checkpoint-receipt.json

python3 scripts/corpus_audit_anchor.py verify \
  --ledger state/acquisition-audit.jsonl \
  --receipt checkpoint-receipt.json \
  --witness-public-key witness-public.pem \
  --as-of 2026-10-06T13:00:00Z
```

For each later anchor, give `prepare` both `--previous-receipt` and its
`--previous-public-key`. Verification accepts repeated `--receipt` and
`--witness-public-key` arguments so a complete chain and planned witness-key
rotation can be checked offline. `--revoked-keys` accepts a JSON string array of
newer revoked key IDs.

## Security boundary and remaining production work

The command accepts public keys and detached signatures only. It has no option
for a witness private key. Tests use ephemeral keys; production keys must remain
inside an independently administered KMS/HSM or equivalent signing service.

This repository implements the checkpoint, receipt, and verification contract.
Production deployment still requires an authenticated witness API, independent
receipt retention, protected trust-bundle and revocation distribution, quorum or
transparency policy where required, operational key rotation, and recovery
exercises. This audit witness is deliberately separate from any trusted-time
anchor.
