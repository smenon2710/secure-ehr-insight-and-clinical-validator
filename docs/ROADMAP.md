# Roadmap

Improvements to make this project production-grade and resume-worthy, in priority order. Reviewers value **measured results** and **production thinking** more than more features, so each item names what to measure.

## 1. Fix Presidio over-redaction, and measure it ✅ Done (2026-10-16)
**Result**: over-redaction 13.8% → 0.0%; PII leak rate 11.1% → 0.0% (added MRN recognizer); patient ID no longer sent to the LLM. See `evals/README.md`.

**Problem (observed)**: Presidio redacts structured field labels and codes. `Route: SC` becomes `<PERSON>: <LOCATION>`: "Route" is read as a name and "SC" (subcutaneous) as South Carolina. The LLM loses clinically useful information.

**Plan**
- Redact free-text fields (`comments`) only. Pass structured coded fields (`drug`, `dose_*`, `route`, `eventtype`, `test_name`, DRG `description`) through.
- Add a clinical allow/deny list for abbreviations (SC, IV, PO/NG, …).
- Drop `None` values from the context (`Furosemide (None None)`).
- Build a small labelled set and report precision and recall before and after.

**Metric**: over-redaction rate, with PII leak rate kept at 0.

## 2. Evaluation harness ✅ Done (2026-10-16)
**Result**: `evals/`: 100% unsafe prompts blocked, 0% false refusals, 100% citation accuracy, ~$0.006/run. Not in CI yet (needs the DB; MIMIC data can't be used as a CI fixture).

~50 golden questions across patients, run in CI:
- **Answer accuracy** against ground truth queried from the DB.
- **Citation accuracy**: cited Record IDs exist and were actually retrieved.
- **Guardrail precision**: block rate on unsafe prompts vs. false refusals on safe ones.
- **PII leak rate** in prompts sent to the LLM.

Publish the numbers in the README.

## 3. Automatic citation verification
After each answer, check that every cited Record ID was in the retrieved set. Flag unverified citations in the UI. (We've seen the model cite record "15" instead of "25".)

## 4. Retrieval quality and performance
- Hybrid search: Postgres full-text (`tsvector`) plus pgvector, merged with reciprocal rank fusion.
- HNSW index on `clinical_embedding`.
- Benchmark p50/p95 latency and recall@5 before and after.

## 5. Compliance features (HIPAA-aligned)
- **Audit log** table: user, question, record IDs accessed, rail fired, timestamp. Add an audit page in the UI.
- **Authentication and role-based access**: clinician login, with per-patient access lists enforced in the API.

## 6. Production engineering
- **Connection pooling**: `get_db_engine()` builds a new engine on every request. Create one at startup and reuse it.
- Replace the deprecated `@app.on_event("startup")` with a FastAPI `lifespan` handler.
- `pytest` unit and API tests, GitHub Actions CI, `docker compose` for local runs.
- HTTPS (ALB or Caddy). Secrets in AWS Secrets Manager instead of `.env`.
- Optional: Terraform for the EC2 instances, security groups and networking.
- Database hardening: restrict `pg_hba.conf`, rotate the `fde_admin` password, or migrate to RDS.

## 7. Presentation
- README with an architecture diagram, a 30-second demo GIF, setup steps and evaluation results.
- Make the repo public only after the database hardening in item 6.

## Resume bullet (items 1 and 2 done)
> Built a zero-trust clinical RAG system over 232K MIMIC-IV records (pgvector, Presidio, NeMo Guardrails, FastAPI, Docker on AWS); built a deterministic evaluation harness and field-aware PII redaction that cut over-redaction from 13.8% to 0% and closed an MRN leak (0% PII leakage), with 100% citation accuracy, 100% unsafe-prompt blocking and 0% false refusals at ~$0.006 per eval run.
