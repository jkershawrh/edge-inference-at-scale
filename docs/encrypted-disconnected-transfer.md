# Encrypted disconnected transfer

Restricted corpus transfer media use the v2 encrypted transfer contract. Every
payload is encrypted independently with AES-256-GCM under a fresh random
256-bit content key. A fresh 96-bit nonce and transfer/layer-specific additional
authenticated data are used for every payload. The content key is wrapped for
the target site's X25519 public key with RFC 9180 HPKE base mode using
HKDF-SHA256 and AES-256-GCM.

Release signing and site encryption are separate roles and keys. The transfer
manifest contains ciphertext digests, recipient key identity, HPKE material,
and the authenticated layer metadata; it never contains the content key,
private key, or plaintext. The completed ciphertext manifest is then signed.

Create a site-scoped transfer (the five artifact flags are all required):

```bash
python3 scripts/export_transfer_set.py /media/evy-transfer \
  --application application.oci \
  --model model.oci \
  --corpus corpus.oci \
  --signature-bundle signatures.json \
  --activation-request activation.json \
  --media-id field-media-014 \
  --event-id flood-response-region-4 \
  --site-id region4-site-alpha \
  --sequence 14 \
  --classification restricted \
  --recipient-public-key /secure/site-alpha-encryption-public.pem \
  --recipient-key-id site-alpha-2026q4 \
  --signing-key /secure/transfer-signing-private.pem
```

At the site, signature, scope, classification, sequence, replay identity,
ciphertext digests, and the closed-world file inventory are verified before a
private key is loaded or any payload is decrypted:

```bash
python3 scripts/verify_transfer_set.py /media/evy-transfer \
  --event-id flood-response-region-4 \
  --site-id region4-site-alpha \
  --allow-classification restricted \
  --sequence-floor 13 \
  --public-key /etc/evy/trust/transfer-signing-public.pem \
  --decryption-key /etc/evy/keys/site-alpha-encryption-private.pem \
  --recipient-key-id site-alpha-2026q4 \
  --decrypt-to /var/lib/evy/staging/release-14
```

Decryption uses a new digest-specific staging directory with restrictive file
permissions, checks each plaintext digest and length, and publishes it by an
atomic rename only after every layer succeeds. Failure removes the temporary
staging directory. Activation remains a separate operation.

Plaintext transfer is allowed for public data. Plaintext non-public transfer
and unsigned transfer require the explicit `--allow-unencrypted-lab` and
`--allow-unsigned-lab` flags. Those modes are testing compatibility and do not
satisfy production policy.

Operational keys are never checked into the repository. Signing keys cannot
decrypt content; site encryption keys cannot sign or authorize releases. Lost,
revoked, or rotated site keys require a newly issued immutable envelope.
