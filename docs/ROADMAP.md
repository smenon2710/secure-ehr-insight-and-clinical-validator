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

## 3. Automatic citation verification (partly done)
**Done**: the UI checks every cited Record ID against the retrieved records and marks unverified ones in amber. **Remaining**: enforce it in the API (e.g. reject or flag the response) so non-UI clients get it too.

After each answer, check that every cited Record ID was in the retrieved set. Flag unverified citations in the UI. (We've seen the model cite record "15" instead of "25".)

## 4. Retrieval quality and performance
- Hybrid search: Postgres full-text (`tsvector`) plus pgvector, merged with reciprocal rank fusion.
- HNSW index on `clinical_embedding`.
- Benchmark p50/p95 latency and recall@5 before and after.

## 5. Compliance features (HIPAA-aligned) ✅ Done (2026-10-16)
**Result**: JWT sign-in, clinician/auditor roles, care-team patient assignments enforced in the API, and an audit log with redacted questions. Includes an auditor dashboard. `evals/security_eval.py` passes 18/18. **Not done**: making the log append-only at the DB level (a separate writer role without UPDATE/DELETE).

- **Audit log** table: user, question, record IDs accessed, rail fired, timestamp. Add an audit page in the UI.
- **Authentication and role-based access**: clinician login, with per-patient access lists enforced in the API.

## 6. Production engineering
- ~~Connection pooling~~ ✅ and ~~`lifespan` instead of `@app.on_event`~~ ✅ (done with item 5).
- `pytest` unit and API tests, GitHub Actions CI, `docker compose` for local runs.
- **HTTPS (planned, not started).** The UI is served over plain HTTP on port 8501, so sign-in passwords and session tokens are unencrypted on the network. Both options need a domain name pointing at the app server.
  - *Option A, Caddy on the app EC2 (recommended; free):*
    - Run Caddy with a two-line Caddyfile (`<domain> { reverse_proxy 127.0.0.1:8501 }`). It obtains and renews a Let's Encrypt certificate automatically and handles Streamlit's websockets.
    - Publish the container on localhost only (`-p 127.0.0.1:8501:8501`).
    - In the security group, open 80 and 443 and remove the public 8501 rule.
  - *Option B, Application Load Balancer + ACM certificate:* managed, but about $16+/month. Use a target group on 8501 with stickiness enabled for websockets.
  - *Verify:* `http://` redirects to `https://`, port 8501 is no longer reachable from outside, and sign-in still works.
- Secrets in AWS Secrets Manager instead of `.env`.
- Optional: Terraform for the EC2 instances, security groups and networking.
- Cost: instances stopped between demos (`docs/DEMO_RUNBOOK.md`), budget alert set. Consider merging PostgreSQL onto the app instance to drop one instance, volume and IP.
- Database hardening: ~~restrict `pg_hba.conf`~~ ✅ and ~~rotate the `fde_admin` password~~ ✅ (2026-10-17); consider migrating to RDS.

## 7. Presentation
- README with an architecture diagram, a 30-second demo GIF, setup steps and evaluation results.
- Make the repo public only after the database hardening in item 6.

## Resume bullet (items 1, 2 and 5 done)
> Built a zero-trust clinical RAG system over 232K MIMIC-IV records (pgvector, Presidio, NeMo Guardrails, FastAPI, Docker on AWS) with role-based access, care-team scoping and a PHI-safe audit trail; built a deterministic evaluation harness and field-aware PII redaction that cut over-redaction from 13.8% to 0% and closed an MRN leak (0% PII leakage), with 100% citation accuracy, 100% unsafe-prompt blocking and 0% false refusals at ~$0.006 per eval run.
