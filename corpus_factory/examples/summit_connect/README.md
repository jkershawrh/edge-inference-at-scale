# Summit Connect sourcing reference

This is the first bounded input set for the agentic corpus-sourcing workflow.
It is synthetic test data, not a field-ready corpus and not evidence that the
system is safe for disaster response.

Inputs:

- `../../fixtures/valid/corpus-mission-profile.json` declares what the event
  pack must answer.
- `source-registry.json` declares the single approved synthetic publisher
  export. Its `.example.test` endpoint is a contract placeholder and is not
  fetched by this example.
- `document-classifications.json` describes only what the checked-in Summit
  documents actually support. Missing facts remain visible as gaps.

Generate the deterministic report without network access:

```bash
python scripts/plan_corpus_coverage.py \
  --mission-profile corpus_factory/fixtures/valid/corpus-mission-profile.json \
  --registry corpus_factory/examples/summit_connect/source-registry.json \
  --classifications corpus_factory/examples/summit_connect/document-classifications.json \
  --as-of 2026-07-02T12:00:00-05:00 \
  --output corpus_factory/examples/summit_connect/coverage-report.json
```

Exit code `1` is expected while required coverage is incomplete. That is the
safe result: the planner recommends bounded source or classification work but
cannot crawl, approve, sign, publish, or deploy anything.
