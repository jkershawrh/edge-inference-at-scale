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
- `lineage-manifest.json` binds the exact bytes and canonical JSON identity of
  all seven inputs. Six are classified; `architecture.json` is explicitly
  excluded from attendee answers rather than silently entering the pack.

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

Turn that report into a ranked, non-executing sourcing queue:

```bash
python scripts/plan_corpus_sourcing.py \
  --mission-profile corpus_factory/fixtures/valid/corpus-mission-profile.json \
  --coverage-report corpus_factory/examples/summit_connect/coverage-report.json \
  --output corpus_factory/examples/summit_connect/sourcing-work-plan.json
```

Rebuild the exact source lineage after any input change (the integration test
then verifies it byte-for-byte):

```bash
python scripts/build_summit_lineage.py \
  --data-dir data/summit_connect \
  --registry corpus_factory/examples/summit_connect/source-registry.json \
  --classifications corpus_factory/examples/summit_connect/document-classifications.json \
  --output corpus_factory/examples/summit_connect/lineage-manifest.json
```

The current report is deliberately not checked in: it is derived evidence and
must be regenerated for the exact planning time and inputs. Its self-validating
digest is what a later promotion evaluation binds.
