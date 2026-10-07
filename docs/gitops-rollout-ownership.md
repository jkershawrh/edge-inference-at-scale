# GitOps and RHACM rollout ownership

Lil EVY now separates reviewed desired release identity from controller-owned
runtime rollout evidence.

The reviewed identity lives in
`chart/profiles/values-gitops-release.yaml`: event ID, release version, promoted
digest, monotonic sequence, policy digest, candidate path, and rollout site.
The RHACM `lil-evy-desired-release` ConfigMap mirrors the fleet-facing subset so
operators can inspect compliance without reading or transporting corpus bytes.
Both files must change in the same reviewed promotion pull request.

The rollout controller alone owns these RAG Deployment pod-template
annotations:

- `lilevy.edge/restart-operation-id`
- `lilevy.edge/target-digest`
- `lilevy.edge/target-sequence`

They are runtime evidence, not Git desired state. The controller writes them
only after it has persisted a bounded rollout operation. Its existing
namespace-scoped Role permits patching only the named RAG Deployment.

## Enforcement layers

The OpenShift GitOps Application enables `RespectIgnoreDifferences=true` and
ignores only the three exact annotation JSON pointers on the one RAG
Deployment. Images, environment variables, release identity, security policy,
and every other Deployment field remain self-healed by GitOps.

A fail-closed `ValidatingAdmissionPolicy` protects the same annotations. An
update may change or remove one only when the authenticated user is
`system:serviceaccount:lil-evy-field:lil-evy-rollout-controller`. GitOps,
RHACM, human operators, and other service accounts may update unrelated fields,
but cannot claim, erase, or replay controller rollout evidence. The binding
selects only namespaces labeled `lilevy.edge/rollout-protected=true` and uses
the `Deny` action.

The RHACM Policy enforces that namespace label, the desired-release ConfigMap,
the admission policy, and its binding on canary clusters selected by all three
labels:

- `lilevy.edge/managed=true`
- `lilevy.edge/event=region4-flood-2026`
- `lilevy.edge/rollout-ring=canary`

It deliberately contains no Deployment object and never writes runtime
annotations. RHACM distributes desired identity and guardrails; the signed
release remains in OCI or disconnected media, and the local rollout controller
performs activation and restart reconciliation.

## Render and apply

Render the application workload before review:

```bash
helm template lil-evy chart \
  --namespace lil-evy-field \
  -f chart/profiles/values-field-safety.yaml \
  -f chart/profiles/values-gitops-release.yaml
```

Install the managed-cluster GitOps and admission resources:

```bash
oc apply -k deploy/gitops/lilevy-rollout
```

Apply the RHACM policy on the hub separately:

```bash
oc apply -f deploy/gitops/lilevy-rollout/rhacm-policy.yaml
```

Before production, replace the example digests, candidate path, site identity,
Git revision, and canary placement values with the exact promoted release. Do
not remove `RespectIgnoreDifferences=true`, broaden the ignored field set, add
the runtime annotations to Git, or configure RHACM to manage the RAG
Deployment. The admission policy intentionally rejects such drift rather than
allowing a GitOps sync to erase live rollout evidence.
