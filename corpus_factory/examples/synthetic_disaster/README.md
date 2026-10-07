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

## Governed software rehearsal

Run the complete disconnected release rehearsal from the repository root:

```bash
python3 scripts/run_synthetic_disaster_rehearsal.py \
  --output artifacts/synthetic-disaster-rehearsal
```

The output directory is immutable: choose a new directory for every run. The
command writes `rehearsal-summary.json`, whose `summary_digest` covers the rest
of the summary, plus the candidate package and the evidence used at each gate.
It exercises the production-shaped contracts and implementations in order:

1. mission, approved registry, `COVERED` coverage, and suitability;
2. exact governed candidate packaging and promotion evaluation;
3. independently approved evaluation attestation and signing authorization;
4. release signing, site-bound encrypted transfer, offline verification, and
   decryption;
5. Lil EVY verification, activation receipt, anti-rollback state, and restart
   persistence; and
6. an emergency RAG-direct answer plus an emergency no-answer refusal.

This is deterministic, synthetic **software evidence**. Retrieval, answer, and
edge-profile measurements are controlled rehearsal inputs, and the runtime
answer checks use mocked retrieval rather than a live field index. The summary
therefore records `SIMULATED_SOFTWARE`; it does not turn those results into
field-readiness evidence.

The same summary records the hardware CUT as `NOT_RUN` and
`NO_HARDWARE_EVIDENCE`. A physical GSM/SMS modem, LoRa radio, antennas and
range, battery/solar runtime, thermal behavior, and target-device power-loss
recovery remain unproven until the corresponding hardware is available.
