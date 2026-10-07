# Fleet identity enrollment and replay persistence

Lil EVY fleet control uses an operator-owned, declarative registry of node
Ed25519 public keys. The reviewed manifests are under
`deploy/gitops/lilevy-fleet`. They can be reconciled directly by OpenShift
GitOps or enforced on a designated RHACM fleet-control cluster.

## Trust boundary

`lil-evy-node-registry` contains exactly one current public-key binding for
each node:

- node ID;
- key ID;
- Ed25519 public key; and
- an explicit `enabled` boolean.

The runtime rejects unknown fields, duplicate JSON fields, malformed or
non-Ed25519 keys, unknown nodes, wrong key IDs, and disabled enrollments. The
closed schema cannot carry a private key, a second ambiguous key, notes, or
unreviewed rotation state. The ConfigMap is mounted read-only in the node
manager. Node private keys remain on their nodes and must never enter Git,
RHACM policy, ConfigMaps, Secrets on the controller, logs, or support bundles.

The two checked-in public keys are synthetic, orphaned placeholders with no
retained private keys. Replace them through review before field deployment.

## Enrollment and rotation

1. Generate the Ed25519 key pair on the node or its approved provisioning
   boundary. Keep the private key there.
2. Transfer only the public key and asserted node/key IDs to the enrollment
   reviewer over an authenticated channel.
3. Add or replace the exact node record in both the base ConfigMap and RHACM
   policy. Repository tests require the byte-identical registry in both places.
4. Obtain the required code-owner/security review and merge the pull request.
5. Wait for GitOps and RHACM compliance before enabling transmissions with the
   enrolled key.

Rotation is intentionally single-key and fail-closed. Pause that node's fleet
messages, replace its key ID and public key in one reviewed change, wait for
compliance, then switch the node to the new private key. The replay floor is
indexed by node ID, so rotation does not reset sequence history; the first
message under the new key must advance the previous node sequence.

For suspected key loss or compromise, first set `enabled: false` and enforce
that emergency change. Re-enrollment uses a new key ID and public key after
incident review. Removing a node record also rejects it, but explicit disabled
records preserve a reviewable retirement decision.

## Durable replay state

The node manager stores per-node sequence high-water marks in SQLite with WAL
and full synchronization on the `lil-evy-fleet-replay` persistent volume. The
database is owner-only and the Deployment uses one replica with `Recreate` and
`ReadWriteOnce`, preventing concurrent SQLite writers. A pod or controller
restart reopens the same database and cannot replay an already accepted
sequence. Deleting or restoring this PVC is a security-sensitive recovery
operation and requires a separate approved procedure; GitOps pruning must not
be used to reset replay state.

The registry is validated during startup. Missing storage, invalid enrollment,
or an invalid authentication mode prevents the control service from becoming
available.

## Exposure and deployment

The base contains no OpenShift Route. Its NetworkPolicy accepts port 8006 only
from the `lil-evy-fleet` namespace. Signed requests authenticate message
integrity; they are not permission to expose the controller publicly. An
approved ingress, mutual-TLS/VPN boundary, rate limits, and site allowlist must
be added by a deployment-specific overlay when intermittent nodes need to
connect.

The base image is pinned to an all-zero, non-runnable digest deliberately.
Replace it with the reviewed backend image digest in a deployment overlay. This
prevents an example manifest from silently pulling a mutable or unapproved
image.

Use one reconciliation authority per cluster. GitOps can deploy the base;
RHACM's `mustonlyhave` policy independently detects and repairs registry drift
and requires the PVC/read-only mount contract. The RHACM Placement selects only
clusters explicitly labeled `lilevy.edge/fleet-control=true`.

Render before review:

```bash
kubectl kustomize deploy/gitops/lilevy-fleet/base
kubectl apply --dry-run=client -k deploy/gitops/lilevy-fleet/base
```
