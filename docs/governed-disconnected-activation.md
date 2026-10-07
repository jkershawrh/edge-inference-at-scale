# Governed disconnected release activation

Governed corpus releases carry their `governed-v1` contract profile in the signed corpus `manifest.json`; the released transfer v1/v2 wire formats remain unchanged. A deployment enables `require_governed_release` during export, import verification, and decryption. Decryption re-derives the profile from the already integrity-verified corpus archive and records it in local staging. The signature bundle is a closed record containing the exact promotion report, signed evaluation attestation, release-signing authorization, and protected release-signature record.

At export, the verifier checks the unsigned corpus archive's exact `manifest.json` bytes against the full signed evidence chain. A governed transfer cannot be created without that verifier. The transfer manifest content-addresses both the corpus and signature bundle. At the field site, `import_governed_encrypted_transfer()` verifies transfer signature, scope, sequence, encryption, and blob integrity; decrypts into temporary staging; and verifies the governed chain before committing inbox replay state or exposing decrypted staging.

`prepare_activation_package()` performs the final transition. It rejects unsafe archives, preinstalled `manifest.sig` files, missing evidence, untrusted keys, stale or revoked attestations, changed candidates, and tampered signatures. Only after every check passes does it extract the package, preserve the four governance records under `governance/`, and write the verified Lil EVY-compatible `manifest.sig` last.

`legacy-v1` remains available only through `allow_legacy_lab=True` at activation preparation. It does not gain governed status and it cannot silently fall through the governed verifier.

Production callers must configure:

- the trusted release key ID and Ed25519 public key;
- trusted evaluation-attestation key IDs and public keys;
- a trusted current time;
- minimum accepted revocation generation; and
- current revoked attestation and attestation-key lists.

No private signing key is used by export, import, decryption, or activation.
