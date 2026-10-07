# Protected release-signing service boundary

Big EVY now has a narrow boundary between governed authorization and release signing. The boundary accepts candidate bytes, a `release_signing_authorization`, its exact promotion report and signed evaluation attestation, and an allowlisted key ID. The signing time comes from the service clock, never the caller. The service verifies the evaluation signature using a separately configured trusted public key, validates revocation state, and checks every authorization digest against the supplied evidence. A fabricated content-addressed authorization is therefore insufficient. It also verifies the exact candidate digest, authorization age, key role, rotation state, release-key revocation generation, and validity window before requesting a signature.

The `ProtectedSignerAdapter` contract exposes only `describe_key(key_id)` and `sign(key_id, payload)`. It has no private-key import, export, or inspection operation. A production adapter should map these calls to PKCS#11 or a KMS `Describe/GetPublicKey` and `Sign` operation. This repository intentionally includes no software-key adapter, private key, cloud-specific integration, or signer deployment that could be mistaken for production custody.

## Rotation and revocation

An operator policy allowlists release key IDs and trusted evaluation-attestation key IDs. Both the authorization and signing key must meet configured revocation-generation floors. Only `active` and `overlap` release keys may sign, so a new and previous key can coexist during a bounded rotation. `retired`, `revoked`, unknown, stale, not-yet-valid, and expired keys fail closed. The durable replay store makes an identical request idempotent and rejects reuse of an authorization/key pair with changed signing inputs.

## HTTP boundary

`create_release_signing_app()` builds an internal FastAPI application with a single bearer-protected `POST /v1/sign-release` route. The request body is bounded and has exactly five fields: `candidate_base64`, `authorization`, `promotion_report`, `evaluation_attestation`, and `key_id`. Errors do not expose adapter or policy details. API documentation endpoints are disabled.

Do not expose this service publicly. Provision its bearer credential from a secret manager and use network policy plus workload identity in OpenShift. An OpenShift profile is intentionally deferred until a real protected adapter and its device/socket permissions are selected; deploying the test adapter would undermine the boundary.

## Offline verification

Verification needs only the original candidate bytes, authorization, signature record, and separately trusted Ed25519 public key:

```bash
python scripts/verify_release_signature.py \
  --candidate release-manifest.json \
  --authorization release-signing-authorization.json \
  --signature release-signature.json \
  --public-key trusted-release-signing-public.pem \
  --manifest-signature-output manifest.sig
```

The verifier checks contract digests, exact candidate and authorization bindings, the governed statement signature, and a second protected-key signature over the exact manifest bytes. Add `--manifest-signature-output manifest.sig` to materialize the latter after all checks pass. Place that file beside the unchanged manifest and configure Lil EVY with the same trusted release public key; it is directly compatible with the existing corpus-package activation verifier. Co-ship `release-signature.json` and the authorization as governance evidence for audit and disconnected transfer.

Trust and revocation policy remain the responsibility of the verifier's trusted configuration; a public key supplied inside an untrusted artifact would not establish identity.
