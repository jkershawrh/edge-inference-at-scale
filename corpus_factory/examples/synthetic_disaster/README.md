# Synthetic disaster suitability drill

This fictional flood-response inventory is a deterministic integration fixture,
not operational guidance. It uses the production version 2 contracts and the
real `evaluate_suitability` gate.

- `policy.json` is an `event_policy` for Region 4 Zone North with exact source
  and publisher allowlists, independent domain/local approvals, and critical
  shelter, water, and evacuation requirements.
- `sources.json` contains valid `SourceRecord` objects. Their evidence digests
  resolve to the immutable text snapshots under `evidence/`.
- `canonical_documents.json` contains valid `CanonicalDocument` objects with
  canonical-text digests and exact source/evidence citation lineage.
- `evaluation_cases.json` uses the suitability inventory contract exactly:
  `positive`, `boundary`, and `no_answer` coverage across English/Spanish and
  SMS/LoRa intersections.

The inventory intentionally retains a neighboring Region 5 shelter distractor
and an expired East Bridge route. The current evacuation document explicitly
supersedes the expired route with West Road. Case IDs identify the neighboring
geography, expired-route, and absent-insulin boundaries even though the compact
suitability case contract carries only coverage metadata.

The integration test selects an incomplete subset of these same records and
proves promotion fails, then evaluates the complete corrected inventory and
proves it passes. No parallel candidate or evaluator format is used.
