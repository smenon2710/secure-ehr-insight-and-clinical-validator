# Changelog

## 2026-10-17: Clinician questions redacted before the LLM; Dockerfile in the repo

### Privacy fix (`src/api/main.py`, `src/pii_redaction/presidio_service.py`)
- **Bug**: only the stored audit-log copy of a question was redacted. The question itself, and earlier user turns, went to the LLM (OpenRouter) as typed, so "Dr. Emily Carter asked…" sent the name out.
- **Fix**: every user turn is Presidio-redacted before it goes into the prompt. Retrieval still embeds the raw question, which is fine because the embedding model runs locally.
- **Avoiding over-redaction**: Presidio on its own turned `Albuterol Inhaler` into `<PERSON>` and `SC` into `<LOCATION>`. At startup the API loads the coded vocabulary from the records (drugs, each drug's leading word, routes, care units, lab tests, admission types) plus common route abbreviations. A detected span is kept only when the whole span matches one of these terms (case-insensitive).
- **Known limits**: brand names that aren't in the data (`Lasix`) are still redacted. A person whose name exactly matches a drug word (`Dr. Senna`) is not redacted.
- `evals/security_eval.py` adds a check that reads the prompts NeMo actually sent: **typed name not sent to the LLM**. It fails on the previous code and passes now (18/18). `rag_eval.py` results are unchanged on a fresh, uncached run.

### Deployment (`Dockerfile`, `.dockerignore`)
- The Dockerfile used on EC2 was never committed. It is now in the repo:
  - **Processes**: API on `127.0.0.1:8000` (container-internal only, so `X-Forwarded-For` can't be spoofed from outside) and Streamlit on `8501`.
  - **If either process exits**, the container exits, so `--restart unless-stopped` restarts it. The container also exits right away if `JWT_SECRET` is missing.
  - **Image contents**: the embedding model is downloaded at build time. The image runs as a non-root user, and `curl` is included for diagnostics.
- `.dockerignore` keeps secrets, MIMIC data and eval output out of the image. It does **not** exclude `scripts/` (see TROUBLESHOOTING 7c).
- The build and run commands are unchanged (see below).

## 2026-10-16: Access control, audit log and UI redesign (roadmap item 5)

### Security (`src/api/`)
- **Sign-in**: bcrypt-hashed passwords; signed JWT session tokens valid for one 8-hour shift (`auth.py`).
- **Roles**, enforced in the API rather than just hidden in the UI:
  - **clinician**: can list, view and query only the patients assigned to them (`patient_access` table).
  - **auditor**: can read the audit log only; no patient data.
- **Audit log** (`audit_log` table, `audit.py`): every sign-in (including failures), patient list, snapshot view, question, denied attempt and audit-log view. Each entry records the user, patient, record IDs accessed, guardrail outcome, latency and IP.
  - Questions are stored **with identifiers removed**, so the log is not a second PHI store.
- Schema in `src/database/security_schema.sql`, applied automatically at API startup.
- New endpoints: `POST /api/v1/auth/login`, `GET /api/v1/auth/me`, `GET /api/v1/audit` (auditors only).
  - All patient endpoints now require a clinician token.
  - `/health` stays open; it returns no patient data.
- Roadmap #6 items fixed along the way:
  - **Connection pooling**: one engine per process instead of one per request.
  - Deprecated `@app.on_event` replaced with a `lifespan` handler.
- `scripts/07_manage_users.py`: create users, assign patients, list and remove users. Passwords are prompted for, never passed as arguments.

### UI (`src/ui/app.py`, `.streamlit/config.toml`)
- Light chart-paper theme; Public Sans with tabular figures.
- **Sign-in screen**. Sessions that expire return you to sign-in with a message.
- **Clinician view**:
  - The patient list shows only your care team.
  - **Evidence chips** under each answer: green if the record was retrieved, amber "unverified" if not (runtime citation check, part of roadmap #3).
  - A table of the records the model saw, with cited rows highlighted.
  - Guardrail blocks get a distinct "Request not answered" callout.
- **Auditor view**: event counts (denied attempts, failed sign-ins, guardrail blocks), a "Needs review" table, filterable event table, and CSV export.

### Evaluation
- `evals/security_eval.py`: 17 allow/deny and audit-trail checks against the real API (17/17 pass, one LLM call).
- `rag_eval.py` now runs as an authorized clinician, tagged `eval_harness` in the audit log. Results unchanged.

### Deploying this version (breaking: sign-in is now required)
1. Add a signing secret to `.env` on the server:
   ```
   python3 -c "import secrets; print('JWT_SECRET=' + secrets.token_urlsafe(48))" >> .env
   ```
2. Pull, rebuild and recreate the container (as before).
3. Create users inside the running container:
   ```
   docker exec -it fde-project-2 python scripts/07_manage_users.py add dr_lee --role clinician --first-n 3
   docker exec -it fde-project-2 python scripts/07_manage_users.py add compliance --role auditor
   ```
   Or run the same commands from the Mac with `uv run python scripts/07_manage_users.py ...`. Users live in the shared database.
4. Deployed and verified on EC2 on 2026-10-17. Two issues hit along the way (script missing from the container, password mismatch) are covered in `docs/TROUBLESHOOTING.md` section 7c.

## 2026-10-16: Field-aware redaction and evaluation harness (roadmap items 1 and 2)

### Redaction (`src/pii_redaction/`)
- **New `record_formatter.py`**, shared by the API and the guardrails retrieval hook.
  - Structured coded fields pass through unredacted. Only free text (`comments`) goes through Presidio.
  - Empty values and MIMIC's `___` placeholders are dropped.
- **The patient ID is no longer sent to the LLM.** The context header used to include it, and Presidio didn't catch it.
- **New MRN recognizer** in `presidio_service.py`. The eval showed labelled MRNs leaking 100% of the time.
- **Results:** over-redaction 13.8% → 0.0%, PII leak rate 11.1% → 0.0%.
  - Before, the old pipeline turned `Record ID: 1970` into `<DATE_TIME>`, `Albuterol Inhaler` into `<PERSON>`, and `Route: SC` into `<PERSON>: <LOCATION>`.

### Evaluation (`evals/`)
- `redaction_eval.py`: over-redaction and PII-leak metrics. No LLM calls.
- `rag_eval.py` + `cases.json`: guardrail accuracy, citation accuracy, grounding, patient-ID leakage.
  - Deterministic checks; responses are cached; exact cost from token counts.
- Results: 100% unsafe prompts blocked, 0% false refusals, 100% citation accuracy, 0 ungrounded drugs, ~$0.006 per full run.
  - Details in `evals/README.md`.

## 2026-10-16: UI redesign and pipeline transparency

### Frontend (`src/ui/app.py`, `.streamlit/config.toml`)
- **Status bar**: live API and database health (`/api/v1/health`), plus badges for each security layer.
- **Patient snapshot sidebar**: age, sex, admissions, vectorized/total records, frequent medications, top diagnoses. Shown to the clinician only, never sent to the LLM.
- **Per-answer pipeline trace**: records retrieved, PII-redacted badge, latency, a **Blocked by NeMo Guardrail** badge when a rail fires, and an expander showing the exact redacted context the LLM received.
- **Suggested questions** on an empty chat, including one that deliberately triggers the medical-advice guardrail.
- **Per-patient conversations** plus a "New conversation" button.
  - **Bug fixed**: chat history used to carry over when switching patients, so patient A's conversation was sent with patient B's request.
- **Dark theme**, a "How it works" panel, and timeouts on all API calls. The API URL can be set with `API_BASE_URL` (default `http://localhost:8000`).

### API (`src/api/main.py`)
- `/api/v1/chat` now also returns `records_retrieved`, `redacted_context`, `guardrail_triggered` and `latency_ms`. The `llm_response` field is unchanged, so existing clients still work.
- The retrieved context now includes each record's `id`, so answers cite real Record IDs instead of the patient ID.
- **New** `GET /api/v1/patients/{patient_id}/summary`: the patient snapshot for the sidebar.
- **New** `GET /api/v1/health`: API, database and model-load status.

### Run locally
```
uv run uvicorn src.api.main:app --reload
uv run streamlit run src/ui/app.py
```

### Redeploy on EC2
```
git pull
docker build -t fde-project-2:latest .
docker rm -f fde-project-2
docker run -d --name fde-project-2 --restart unless-stopped --env-file .env -p 8501:8501 fde-project-2:latest
```
