# Evaluation attestation and protected release-signing handoff

Governed corpus releases now have an executable two-phase trust boundary:

```text
unsigned candidate → exact-digest evaluation → independent approvals
→ external attestation signature → exact-digest release-signing authorization
→ protected corpus-release signer
```

This ordering matters. A package signature proves only that a key signed bytes;
it cannot prove those bytes passed retrieval, grounded-answer, safety, coverage,
and edge-profile gates. The evaluation attestation binds the complete promotion
report and its exact candidate release digest before a release signer is allowed
to act.

## Enforced contract

An `evaluation_attestation` contains:

- the promotion report ID and digest of the complete report;
- the evaluated candidate release digest, governed contract profile, PASS
  decision, and evaluation-spec version;
- distinct evaluation-executor, evaluation-owner, release-approver, and signing
  service identities;
- distinct approval independence groups and content-addressed approval bodies;
- attestation and Ed25519 key validity windows;
- the signing key role and key ID, calculated as lowercase SHA-256 of the DER
  SubjectPublicKeyInfo, as required by the trust-model ADR;
- the revocation snapshot generation, check time, and explicit attestation/key
  revocation state; and
- a detached Ed25519 signature over canonical statement bytes.

Validation fails closed for report or candidate drift, a failed promotion,
identity or independence-group reuse, approval after signing, signing outside an
attestation/key validity window, malformed signatures, expired evidence, key-ID
mismatch, embedded or external revocation, and any schema ambiguity.

The resulting `release_signing_authorization` binds the candidate, promotion
report, attestation, attestation key, and revocation generation. Its only action
is `sign_exact_candidate_digest` with a required `corpus-release` key role.
It also emits `evaluation_report_digest` and `approval_attestation_digests` as
the exact fields the protected release signature statement must cover.

## External signing workflow

Prepare the exact bytes for a protected evaluation-attestation signer:

```bash
python3 scripts/prepare_evaluation_attestation.py \
  --promotion-report promotion-report.json \
  --approvals evaluation-approvals.json \
  --evaluation-executor-identity urn:example:service:evaluator \
  --valid-from 2026-10-06T18:00:00Z \
  --expires-at 2026-10-07T18:00:00Z \
  --revocation-generation 7 \
  --revocation-checked-at 2026-10-06T18:09:00Z \
  --signer-identity urn:example:service:attestation-signer \
  --signed-at 2026-10-06T18:15:00Z \
  --public-key evaluation-attestation-public.pem \
  --key-valid-from 2026-10-01T00:00:00Z \
  --key-expires-at 2027-01-01T00:00:00Z \
  --output evaluation-attestation-statement.json \
  --payload-output evaluation-attestation.payload
```

Send only `evaluation-attestation.payload` to the external KMS/HSM or signing
service. After it returns a raw or base64 Ed25519 signature:

```bash
python3 scripts/finalize_evaluation_attestation.py \
  --statement evaluation-attestation-statement.json \
  --signature evaluation-attestation.signature \
  --public-key evaluation-attestation-public.pem \
  --output evaluation-attestation.json

python3 scripts/authorize_corpus_signing.py \
  --candidate-manifest candidate/manifest.json \
  --promotion-report promotion-report.json \
  --evaluation-attestation evaluation-attestation.json \
  --attestation-public-key evaluation-attestation-public.pem \
  --as-of 2026-10-06T19:00:00Z \
  --output release-signing-authorization.json
```

Optional JSON arrays supplied through `--revoked-attestations` and
`--revoked-keys` apply newer external revocation state at authorization time.

## Key-custody boundary

These commands never accept an evaluation-attestation private key or a corpus
release private key. They prepare payloads and verify public-key signatures.
The governed packaging command also rejects its legacy `--signing-key` option;
governed candidates remain unsigned until the independent evaluation attestation
authorizes the external `corpus-release` signer. Legacy lab packaging retains
its compatibility option.

This is a tested contract and handoff, not a production KMS/HSM implementation.
Production still requires protected key generation and custody, authenticated
signing-service policy, independently distributed revocation snapshots, audit
anchoring, rotation exercises, and offline trust-root operations.
