# Fleet control-plane authentication

Fleet registration and heartbeat endpoints are signed by default. Each field
node has an Ed25519 identity enrolled by an operator; arbitrary callers cannot
register a node, forge its corpus state, or replay an old healthy heartbeat.
This can be tested entirely on OpenShift before radio hardware exists.

## Trust boundary

The controller reads a mounted, operator-owned registry from
`FLEET_NODE_REGISTRY_PATH` (default
`/etc/lil-evy/fleet/nodes.json`). The registry binds one enabled node ID to one
key ID and Ed25519 public key:

```json
{
  "schema_version": 1,
  "nodes": {
    "field-001": {
      "key_id": "field-001-2026q4",
      "public_key_pem": "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n",
      "enabled": true
    }
  }
}
```

Private keys stay on their nodes. Registry changes, including disablement and
rotation, belong in the deployment GitOps review path; the API cannot enroll or
modify identities.

## Signed wire contract

`POST /nodes/register` and `POST /nodes/heartbeat` include an `auth` object with
`key_id`, Unix `issued_at`, monotonically increasing `sequence`, and a base64
Ed25519 `signature`. The signature covers a canonical, domain-separated JSON
envelope containing the message kind, node ID, proof metadata, and the entire
validated request excluding `auth`.

The controller checks node/key enrollment, enablement, timestamp window, exact
signature, and sequence. A SQLite high-water mark at
`FLEET_REPLAY_STATE_PATH` survives process restarts and advances atomically.
Mount that path on persistent storage and back it up; losing it weakens replay
protection until the node advances beyond its former sequence.

Heartbeat metrics are a small allowlist of counts, latency, queue depth, and
load. Unknown fields are rejected so message text, retrieved documents, phone
numbers, and other user data cannot leak into fleet inventory.

## Modes and limitations

- `FLEET_AUTH_MODE=required` is the default and fails closed.
- `FLEET_AUTH_MODE=lab` permits unsigned requests only for the local three-node
  simulator. It must not be used in a field deployment.
- `FLEET_MAX_CLOCK_SKEW_SECONDS` defaults to 300. A disconnected trusted-time
  strategy is still required before production field qualification.
- Transport TLS/mTLS, secret provisioning, registry GitOps policy, and RHACM
  integration remain deployment responsibilities; message signing does not
  replace them.
