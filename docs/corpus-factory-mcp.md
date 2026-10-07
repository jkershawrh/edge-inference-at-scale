# Big EVY Corpus Factory MCP

Big EVY exposes a deliberately small Model Context Protocol (MCP) server for
connected-lab agents and operator clients. It is an interoperability boundary
over the existing deterministic corpus contracts; it is not an autonomous
publisher and is never part of Lil EVY's SMS request path.

The implementation uses the official Python SDK v2 `MCPServer` API. The SDK is
constrained to the current stable major line in `corpus_factory/requirements.txt`.
See the official [Python SDK installation guide](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/get-started/installation.md),
[tool guide](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/servers/tools.md),
and [transport guide](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/index.md).

## Exposed tools

- `get_mission_status`: mission identity, scope, validity, requirements, and
  configured source/classification counts.
- `plan_coverage`: deterministic coverage, gap, and conflict evaluation at a
  caller-supplied, timezone-aware timestamp.
- `generate_sourcing_work_plan`: at most 25 ranked advisory work items derived
  from current gaps.
- `verify_lineage`: exact-byte verification of the configured lineage manifest
  against the configured local evidence directory.

Every tool is declared read-only, idempotent, and closed-world. Those MCP
annotations help clients, but enforcement lives in the server: tools accept no
paths, URLs, source content, commands, credentials, approval records, or release
instructions.

The server has no capability for network fetching, arbitrary file access,
source approval, signing, publication, deployment, or shell execution. It reads
only exact operator-configured artifacts resolved beneath allowlisted roots.
Inputs, file counts, artifact sizes, output size, and work-item counts are
bounded. Invalid contracts, escaped paths, symlinks leaving the evidence
directory, and lineage mismatches fail closed.

## Local use

Install the connected factory dependencies with Python 3.11 or later:

```bash
python3.11 -m pip install -r corpus_factory/requirements.txt
python3.11 -m corpus_factory.mcp_server
```

The default transport is stdio and opens no listening port. An MCP-capable host
should launch the second command as its local server process. The checked-in
defaults select only the synthetic Summit Connect reference set.

Run the focused contract and in-process protocol tests with:

```bash
make test-corpus-mcp
```

## Configuration

Configuration belongs to the operator, not the MCP client:

| Variable | Default relative to the first allowed root |
| --- | --- |
| `BIG_EVY_MCP_ALLOWED_ROOTS` | repository root |
| `BIG_EVY_MCP_MISSION_PROFILE` | `corpus_factory/fixtures/valid/corpus-mission-profile.json` |
| `BIG_EVY_MCP_SOURCE_REGISTRY` | `corpus_factory/examples/summit_connect/source-registry.json` |
| `BIG_EVY_MCP_CLASSIFICATIONS` | `corpus_factory/examples/summit_connect/document-classifications.json` |
| `BIG_EVY_MCP_LINEAGE_MANIFEST` | `corpus_factory/examples/summit_connect/lineage-manifest.json` |
| `BIG_EVY_MCP_EVIDENCE_DIR` | `data/summit_connect` |

`BIG_EVY_MCP_ALLOWED_ROOTS` is an OS-path-separator-delimited list. Relative
artifact paths resolve beneath its first entry. Absolute artifact paths are
accepted only when they resolve beneath one of the configured roots. Do not use
a home directory or another broad shared directory as an allowed root.

## Container and OpenShift use

Build the dedicated connected-side image from the repository root:

```bash
podman build -f corpus_factory/Containerfile.mcp -t big-evy-corpus-mcp .
```

The image starts the SDK's Streamable HTTP transport on `0.0.0.0:8006`; a local
source launch binds to `127.0.0.1` unless `BIG_EVY_MCP_HOST` is explicitly
changed. On OpenShift, place the container behind an authenticated service proxy
and a namespace-restricted NetworkPolicy. Do not create a public Route directly
to this unauthenticated process.

This service is connected-side only and optional. It does not consume Lil EVY
field-node headroom. A practical initial OpenShift request is 50m CPU and 128Mi
memory, with a 250m CPU and 256Mi memory limit; actual requests must be adjusted
from measured pod telemetry. The service performs no model inference, embedding,
or vector indexing, so its idle footprint should be small relative to the RAG
runtime.

A hardened internal-only baseline is provided at
`deploy/corpus-factory-mcp/base`. It runs without a service-account token, with
a read-only root filesystem, all Linux capabilities dropped, default-deny
ingress and egress, and a ClusterIP Service only. There is deliberately no
Route. Before applying it, set the reviewed immutable image digest; the checked-in
all-zero digest is an intentional fail-closed placeholder. Only same-namespace
client pods labeled `lilevy.edge/mcp-client=approved` can connect:

```bash
cd deploy/corpus-factory-mcp/base
kustomize edit set image \
  quay.io/replace-me/big-evy-corpus-mcp=quay.io/your-org/big-evy-corpus-mcp@sha256:REVIEWED_DIGEST
cd ../../..
oc apply -k deploy/corpus-factory-mcp/base
```

That label is an authorization boundary only when namespace write access is
restricted. For people or clients outside the namespace, keep the server
internal and add the organization's authenticated service proxy; do not weaken
the NetworkPolicy or expose the unauthenticated MCP process directly.
