# Lil EVY field grounding mode

## Why this exists

The conference demo historically treated RAG as preferred context: if retrieval
was unavailable or returned no evidence, the router could still ask the local
LLM for an answer. It also returned a conference-specific emergency template
for words such as `fire`, `medical`, and `evacuation`.

That behavior is unsuitable for disaster, conflict, rural-service, or other
high-consequence deployments. A language model must not invent operational
guidance when the active local corpus cannot support it, and a node must not
assume that a particular national emergency number or conference security desk
exists.

## Field-safe behavior

Apply the field safety overlay alongside the selected hardware profile:

```bash
helm upgrade --install edge-inference ./chart \
  -f chart/profiles/values-lab-balanced.yaml \
  -f chart/profiles/values-field-safety.yaml
```

The overlay enables two independent controls:

- `RAG_GROUNDING_REQUIRED=true`: ordinary free-text questions with no retrieved
  evidence receive a short no-answer response. The LLM is not called.
- `EMERGENCY_RAG_ENABLED=true`: emergency-classified questions retrieve approved
  event guidance and never call the LLM. A sufficiently confident document is
  returned directly and may be split into SMS segments. If current guidance is
  missing or below the retrieval threshold, the node returns a generic refusal
  directing the user to trusted local instructions or responders.

Emergency turns are not added to conversational history. The refusal messages
fit within one SMS and intentionally contain no assumed phone number, location,
agency, or route.

## Deployment requirement

Field grounding mode must be combined with a signed, event-specific corpus and
the applicable event suitability policy. Enabling the flags alone does not make
sample conference data operationally appropriate. Before deployment, verify:

- `rag.corpus.enabled=true` with an immutable image digest;
- `rag.corpus.requireSignature=true` and a provisioned public-key secret;
- the correct event ID and corpus version;
- retrieval thresholds approved for the event safety classes; and
- positive, boundary, stale, neighboring-geography, and no-answer tests.

The legacy defaults remain available for conference demonstrations and model
experiments. They must not be used as a field safety profile.
