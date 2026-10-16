# Changelog

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
