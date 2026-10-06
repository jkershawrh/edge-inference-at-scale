# ADR 0002: Corpus signing and encryption envelope

- Status: Proposed for Wave 1 contract freeze
- Date: 2026-10-06
- Depends on: ADR 0001
- Scope: Corpus Release v2 and transfer sets

## Context

Corpus releases may pass through untrusted registries, mirrors, field laptops,
and removable media. All releases need origin authentication and integrity.
Some releases also contain sensitive operational data and need confidentiality
in transit and at rest before activation.

Encryption must not hide the identity of the artifact being authorized, and a
decryption failure must not expose partially trusted content to the indexer.
Signing and encryption also need independent rotation and revocation paths.

## Decision

### Sign every release; encrypt only when classification requires it

Every production Corpus Release is an immutable OCI artifact identified by its
manifest digest. Registry tags are mutable aliases and are never signed or
activated as identities.

Big EVY creates a canonical ReleaseManifest, stores it as an OCI layer, and
signs a statement containing at least:

- the exact OCI manifest digest and ReleaseManifest digest;
- event ID, release UUID, semantic version, and monotonic sequence;
- site/deployment scope and data classification;
- approval, evaluation, policy, and trust-bundle digests;
- signing key ID, signature algorithm, and signing time; and
- a critical-field declaration that makes unknown security fields fail closed.

The signature statement is serialized with deterministic JSON using RFC 8785
JSON Canonicalization Scheme and signed with Ed25519. The existing v1 detached
`manifest.sig` is accepted only by an explicit legacy policy and cannot satisfy
a v2 production policy.

Lil EVY first verifies the outer OCI digest and release signature, then all
layer digests and sizes, before parsing canonical documents or building an
index. The artifact evaluated in Big EVY is byte-for-byte the artifact signed
and promoted.

### Confidentiality uses per-release envelope encryption

For a confidential release, Big EVY:

1. generates a new random 256-bit content-encryption key for that release;
2. encrypts each confidential OCI payload layer independently with AES-256-GCM
   and a fresh random 96-bit nonce that is never reused with the key;
3. binds the release UUID, layer media type, plaintext digest, ciphertext
   digest, and layer ordinal as authenticated additional data;
4. wraps the content-encryption key separately for every authorized site using
   HPKE RFC 9180 with X25519, HKDF-SHA256, and AES-256-GCM; and
5. signs the ciphertext OCI artifact digest and envelope metadata.

Plaintext digests are inside the signed, encrypted metadata. Ciphertext digests
and non-secret recipient key IDs are visible in the signed outer manifest. No
private key or unwrapped content key is written to the release, registry, logs,
receipts, or general build workspace.

Encryption recipients are sites or deployments, not individual nodes, unless
policy requires node-specific containment. Re-wrapping a content key for a new
recipient produces a new envelope artifact and audit event; it never mutates an
existing release digest.

### Verification precedes decryption, and decryption precedes parsing

Lil EVY applies this order:

1. verify outer digest, signature chain, key role, scope, revocation, sequence,
   and recipient identity;
2. unwrap the content key within the site keystore;
3. decrypt into a digest-specific staging area with restrictive permissions;
4. verify authenticated data and plaintext digests;
5. validate schemas and policies, index, and smoke-test; then
6. atomically activate.

Any failure destroys the staged plaintext and key material, records a
non-sensitive reason code, and leaves the active release unchanged. Decrypted
staging data is never placed on transfer media or in the registry.

### Signing and encryption lifecycles are independent

- Release authenticity remains verifiable after a site encryption key rotates.
- Site encryption-key rotation does not authorize release signing.
- Release-signing-key revocation does not itself erase already activated
  plaintext; release revocation policy determines continued service.
- Loss of a site decryption key requires re-wrapping or reissuing the envelope,
  not rebuilding canonical content.
- Backups of encryption keys follow the data-classification policy and are not
  stored with encrypted media.

### OCI is the carrier, not the trust root

Connected pulls, resumable mirrored pulls, and OCI-layout physical-media
imports use the same digest and signature. A disconnected transfer set also
includes the required application, model, trust-bundle, revocation-snapshot,
and activation-request digests. The transfer-set inventory is signed, but each
contained artifact is still verified independently.

## Consequences

- Public corpora incur signature overhead but no unnecessary encryption cost.
- Confidential packages can traverse untrusted storage without trusting that
  storage for secrecy or integrity.
- Site compromise does not expose releases encrypted only for other sites.
- Recipient changes create additional immutable artifacts and inventory entries.
- Lost site keys can make encrypted content unrecoverable unless the approved
  backup or re-wrap procedure exists.
- Secure deletion on flash storage is not assumed; disk encryption and staging
  lifecycle controls are mandatory for confidential content.

## Required records and tests

- Golden fixtures cover modified ciphertext, modified envelope metadata,
  wrong recipient, reused or invalid nonce metadata, missing layer, reordered
  layer, plaintext digest mismatch, wrong site, invalid signature, and successful
  online/offline delivery of the same digest.
- Cryptographic known-answer tests pin serialization and signature behavior
  across Big EVY and Lil EVY implementations.
- Logs and activation receipts are tested to ensure they contain no plaintext,
  content keys, private keys, user queries, or sensitive source locations.

## Unresolved decisions

- Whether the OCI signature attachment uses a cosign-compatible envelope,
  Notation, or a project media type while retaining the signed statement above.
- Which OCI registry and disconnected mirroring stack becomes the supported
  production profile.
- Whether restricted emergency sites require per-node rather than per-site
  recipients.
- Memory-only versus encrypted-disk staging limits for each Lil EVY profile.
