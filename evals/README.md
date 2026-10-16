# Evaluations

Two scripts. Both use deterministic checks: no LLM-as-judge, and only one of them calls the LLM at all.

| Script | What it measures | LLM calls | Runtime |
|---|---|---|---|
| `redaction_eval.py` | Over-redaction of structured fields, and PII leakage | **0** | ~20 s |
| `rag_eval.py` | Guardrail accuracy, citation accuracy, grounding, patient-ID leakage | ~2 per case, **cached** | ~75 s cold |

Both need the database reachable (`.env`). Results are written to `evals/results/` and cached responses to `evals/.cache/`. Both are gitignored because they contain MIMIC-derived text.

## Run
```
uv run python evals/redaction_eval.py            # --sample 300 (default)
uv run python evals/rag_eval.py                  # reuses cached responses: free
uv run python evals/rag_eval.py --no-cache       # fresh LLM calls (~$0.006 for all 14 cases)
uv run python evals/rag_eval.py --no-cache --limit 2   # cheap smoke test
```

## How the checks work
**Redaction** (300 real records, sampled deterministically)
- *Over-redaction*: share of structured values (`Record ID`, `Drug`, `Route`, `Event`, `Lab test`, `Diagnosis`) that no longer appear intact after redaction. Old pipeline vs new.
- *PII leakage*: 9 synthetic PII probes (names, phone, email, SSN, date, location, hospital, MRN) are injected into the free-text `comments` field of 10 records each. A probe leaks if its value appears in the output.

**RAG** (`cases.json`: 8 safe questions, 6 unsafe, across the 6 embedded patients; patients are referenced by position, so no IDs are committed)
- *Guardrails*: unsafe prompts must be blocked (block rate), safe ones must not be (false-refusal rate).
- *Citation accuracy*: every Record ID cited in an answer must be in the retrieved context.
- *Grounding*: every drug named in an answer (matched against the DB's drug vocabulary) must appear in the retrieved context.
- *Privacy*: the patient's ID must not appear in the context sent to the LLM.
- *Cost*: NeMo's per-call token counts × OpenRouter's published price for the configured model.

Responses are cached under a fingerprint of the files that shape the prompt (`main.py`, guardrails config, redaction code). Changing any of them invalidates the cache automatically.

## Latest results (2026-10-16, `deepseek/deepseek-v3.2`)

**Redaction**

| Metric | Before | After |
|---|---|---|
| Over-redaction of structured fields | 13.8% | **0.0%** |
| Diagnosis values kept intact | 45.3% | **100%** |
| PII leak rate (90 injections) | 11.1% (MRNs) | **0.0%** |
| Patient ID sent to LLM | yes | **no** |

**RAG (14 cases)**

| Metric | Result |
|---|---|
| Unsafe prompts blocked | **100%** (6/6) |
| False refusals on safe prompts | **0%** (0/8) |
| Citation accuracy | **100%** |
| Answers with a citation | **100%** |
| Ungrounded drug mentions | **0** |
| Patient ID leaks | **0** |
| Cost per full run | ~$0.006 (~$0.00045 per case) |

## Limitations
- 14 cases and 6 patients is a small set. Treat these as regression checks, not statistical claims.
- Grounding only checks drug names, not doses or other values.
- The MRN recognizer needs a label (`MRN`, `MR#`, `medical record number`). A bare number is not caught.
- `scripts/06_test_guardrails.py` (global retrieval) is not covered. It has mis-cited a Record ID ("15" for "25"). Runtime citation checking (roadmap #3) would catch that.
