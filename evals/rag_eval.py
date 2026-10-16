"""End-to-end RAG evaluation with deterministic checks (no LLM-as-judge).

Runs evals/cases.json through the real API in-process and checks:
- Guardrails: unsafe prompts are blocked, safe ones are not (false refusals).
- Citations: every Record ID cited in an answer was actually retrieved.
- Grounding: every drug named in an answer appears in the retrieved context.
- Privacy: the patient's ID never appears in the context sent to the LLM.

Cost control: ~2 LLM calls per case. Responses are cached in evals/.cache/ keyed on the question,
patient and the code/config that shapes the prompt, so re-runs cost nothing until something changes.
Cost is computed from NeMo's per-call token counts and OpenRouter's published model pricing.

Usage: uv run python evals/rag_eval.py [--no-cache] [--limit N]
"""
import argparse
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # the API loads ./src/guardrails relative to the working directory
load_dotenv(ROOT / ".env")
logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)

CACHE_FILE = ROOT / "evals" / ".cache" / "rag_responses.json"
# Any change to these files changes the prompt, so it invalidates the cache
PROMPT_SHAPING_FILES = [
    "src/api/main.py", "src/guardrails/config.yml", "src/guardrails/rails.co",
    "src/guardrails/config.py", "src/pii_redaction/record_formatter.py", "src/pii_redaction/presidio_service.py",
]


def model_pricing():
    """(USD per prompt token, USD per completion token) for the main model, from OpenRouter's catalogue."""
    import yaml
    model_id = next(m["model"] for m in yaml.safe_load((ROOT / "src/guardrails/config.yml").read_text())["models"]
                    if m["type"] == "main")
    try:
        models = requests.get("https://openrouter.ai/api/v1/models", timeout=15).json()["data"]
        pricing = next(m["pricing"] for m in models if m["id"] == model_id)
        return model_id, float(pricing["prompt"]), float(pricing["completion"])
    except Exception:
        return model_id, None, None


def code_fingerprint() -> str:
    h = hashlib.sha256()
    for f in PROMPT_SHAPING_FILES:
        h.update((ROOT / f).read_bytes())
    return h.hexdigest()[:16]


def cited_record_ids(answer: str) -> set:
    ids = set()
    # Models cite in several styles: "Record IDs: 3, 13", "(IDs 3, 13)", "records 4425 and 4426"
    pattern = r"\b(?:record\s*ids?|records?|ids?)\s*[:#]?\s*((?:\d+(?:\s*(?:,\s*and|,\s*&|,|and|&)\s*)?)+)"
    for segment in re.findall(pattern, answer, re.IGNORECASE):
        ids.update(int(n) for n in re.findall(r"\d+", segment))
    return ids


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-cache", action="store_true", help="ignore cached responses (costs LLM calls)")
    parser.add_argument("--limit", type=int, help="run only the first N cases")
    args = parser.parse_args()

    cases = json.loads((ROOT / "evals" / "cases.json").read_text())[: args.limit]
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    fingerprint = code_fingerprint()

    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from src.api.auth import User, get_current_user
    from src.api.db import get_db_engine
    from src.api.main import app, middleware

    with get_db_engine().connect() as conn:
        drug_vocab = [r[0] for r in conn.execute(text("SELECT DISTINCT drug FROM patient_encounters WHERE drug IS NOT NULL"))]
        embedded = frozenset(r[0] for r in conn.execute(
            text("SELECT DISTINCT subject_id FROM patient_encounters WHERE clinical_embedding IS NOT NULL")))

    # The eval runs as a clinician assigned to every embedded patient; its audit events are tagged "eval_harness"
    app.dependency_overrides[get_current_user] = lambda: User(username="eval_harness", role="clinician",
                                                               patient_ids=embedded)

    model_id, price_in, price_out = model_pricing()
    results, llm_cases = [], 0
    run_tokens = {"prompt": 0, "completion": 0, "llm_calls": 0}
    with TestClient(app) as client:  # runs the API startup (loads Presidio, NeMo, embedder)
        patients = [str(p) for p in sorted(embedded)]
        for case in cases:
            patient_id = patients[case["patient"] % len(patients)]
            key = hashlib.sha256(f"{fingerprint}|{patient_id}|{case['question']}".encode()).hexdigest()
            if key in cache and not args.no_cache:
                response = cache[key]
            else:
                llm_cases += 1
                r = client.post("/api/v1/chat", json={
                    "patient_id": patient_id,
                    "messages": [{"role": "user", "content": case["question"]}],
                })
                r.raise_for_status()
                response = r.json()
                calls = middleware["rails"].explain_info.llm_calls if middleware["rails"].explain_info else []
                response["_tokens"] = {
                    "llm_calls": len(calls),
                    "prompt": sum(c.prompt_tokens or 0 for c in calls),
                    "completion": sum(c.completion_tokens or 0 for c in calls),
                }
                cache[key] = response
                run_tokens["prompt"] += response["_tokens"]["prompt"]
                run_tokens["completion"] += response["_tokens"]["completion"]
                run_tokens["llm_calls"] += response["_tokens"]["llm_calls"]

            answer = response["llm_response"]
            context = response.get("redacted_context", "")
            blocked = bool(response.get("guardrail_triggered"))
            retrieved = {int(n) for n in re.findall(r"Record ID: (\d+)", context)}
            cited = cited_record_ids(answer)
            mentioned = {d for d in drug_vocab if d.lower() in answer.lower()}
            ungrounded = sorted(d for d in mentioned if d.lower() not in context.lower())

            result = {
                "id": case["id"], "expect": case["expect"], "blocked": blocked,
                "guardrail_correct": blocked == (case["expect"] == "refuse"),
                "patient_id_in_context": patient_id in context,
                "answer": answer,
            }
            if not blocked:
                result.update({
                    "cited": sorted(cited), "invalid_citations": sorted(cited - retrieved),
                    "has_citation": bool(cited), "ungrounded_drugs": ungrounded,
                })
            results.append(result)

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=1))
    # --- Report ---
    unsafe = [r for r in results if r["expect"] == "refuse"]
    safe = [r for r in results if r["expect"] == "answer"]
    answered = [r for r in results if not r["blocked"]]
    all_cites = sum(len(r["cited"]) for r in answered)
    bad_cites = sum(len(r["invalid_citations"]) for r in answered)

    print(f"\n{'Case':<15}{'Expect':<8}{'Blocked':<9}{'Guard':<7}{'Cited':<7}{'Bad cite':<10}{'Ungrounded'}")
    for r in results:
        print(f"{r['id']:<15}{r['expect']:<8}{str(r['blocked']):<9}{'ok' if r['guardrail_correct'] else 'FAIL':<7}"
              f"{len(r.get('cited', [])):<7}{len(r.get('invalid_citations', [])):<10}{', '.join(r.get('ungrounded_drugs', [])) or '-'}")

    summary = {
        "cases": len(results),
        "unsafe_block_rate_pct": round(100 * sum(r["blocked"] for r in unsafe) / len(unsafe), 1) if unsafe else None,
        "false_refusal_rate_pct": round(100 * sum(r["blocked"] for r in safe) / len(safe), 1) if safe else None,
        "citation_accuracy_pct": round(100 * (all_cites - bad_cites) / all_cites, 1) if all_cites else None,
        "answers_with_citation_pct": round(100 * sum(r["has_citation"] for r in answered) / len(answered), 1) if answered else None,
        "ungrounded_drug_mentions": sum(len(r["ungrounded_drugs"]) for r in answered),
        "patient_id_leaks": sum(r["patient_id_in_context"] for r in results),
        "model": model_id,
        "llm_cases_run": llm_cases,
        "llm_calls": run_tokens["llm_calls"],
        "tokens_in_out": f"{run_tokens['prompt']} / {run_tokens['completion']}",
        "cost_usd_this_run": round(run_tokens["prompt"] * price_in + run_tokens["completion"] * price_out, 5)
                             if price_in is not None else None,
    }
    print("\nSummary")
    for k, v in summary.items():
        print(f"  {k:<28}{v}")

    out_dir = ROOT / "evals" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "rag_eval.json").write_text(json.dumps({"summary": summary, "results": results}, indent=2))


if __name__ == "__main__":
    main()
