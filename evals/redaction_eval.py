"""Redaction evaluation: no LLM calls, no cost.

1. Over-redaction: on a fixed sample of real records, how many structured values (route, drug, dose...)
   survive redaction intact, comparing the old pipeline (Presidio over the whole line) with the new
   field-aware formatter (src/pii_redaction/record_formatter.py).
2. PII leakage: inject synthetic PII into the free-text field and check none of it reaches the output.

Usage: uv run python evals/redaction_eval.py [--sample 300]
"""
import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.pii_redaction.presidio_service import ClinicalPIIRedactor
from src.pii_redaction.record_formatter import format_record

logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)

# Fields compared between pipelines, with the label each pipeline uses
COMPARED_FIELDS = {
    "id": ("Record ID", "Record ID"),
    "drug": ("Drug", "Drug"),
    "route": ("Route", "Route"),
    "eventtype": ("Event", "Event"),
    "test_name": ("Test", "Lab test"),
    "description": ("Diagnosis", "Diagnosis"),
}

# Synthetic PII injected into the free-text comments field: (type, text to inject, value that must not leak)
PII_PROBES = [
    ("PERSON", "Discussed with Dr. Emily Carter.", "Emily Carter"),
    ("PERSON", "Patient Johnathan Reyes was notified.", "Johnathan Reyes"),
    ("PHONE", "Call back at 617-555-0143.", "617-555-0143"),
    ("EMAIL", "Results sent to m.chen@example.org.", "m.chen@example.org"),
    ("SSN", "SSN 512-44-7789 on file.", "512-44-7789"),
    ("DATE", "Specimen collected on March 3, 2021.", "March 3, 2021"),
    ("LOCATION", "Transferred from Springfield, Illinois.", "Springfield"),
    ("ORGANIZATION", "Sent out to Mayo Clinic for confirmation.", "Mayo Clinic"),
    ("MRN", "MRN 4482913 verified at registration.", "4482913"),
]

SELECT_COLUMNS = """id, subject_id, admission_type, careunit, eventtype, drug, prod_strength, dose_val_rx,
                    dose_unit_rx, route, test_name, comments, description, drg_severity"""


def old_pipeline(row, redactor) -> str:
    """The original API formatting: every field (including None) in one line, Presidio over all of it."""
    line = (
        f"Record ID: {row['id']}, Drug: {row['drug']} ({row['dose_val_rx']} {row['dose_unit_rx']}), Route: {row['route']}, "
        f"Event: {row['eventtype']}, Test: {row['test_name']}, Comments: {row['comments']}, Diagnosis: {row['description']}"
    )
    return redactor.redact_clinical_context(line)


def clean(value):
    return None if value is None or str(value).strip() in ("", "___") else " ".join(str(value).split())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=300, help="records to sample (deterministic)")
    parser.add_argument("--probe-records", type=int, default=10, help="records per PII probe")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    engine = create_engine(
        f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT {SELECT_COLUMNS} FROM patient_encounters
            WHERE clinical_embedding IS NOT NULL
            ORDER BY md5(id::text) LIMIT :n
        """), {"n": args.sample}).mappings().fetchall()

    redactor = ClinicalPIIRedactor()

    # --- 1. Over-redaction ---
    kept = {"old": defaultdict(int), "new": defaultdict(int)}
    total = defaultdict(int)
    damaged_examples = []
    for row in rows:
        old_out = old_pipeline(row, redactor)
        new_out = format_record(row, redactor)
        for field, (old_label, new_label) in COMPARED_FIELDS.items():
            value = clean(row[field])
            if value is None:
                continue
            total[field] += 1
            # Old pipeline didn't normalise whitespace; compare on normalised output
            if f"{old_label}: {value}" in " ".join(old_out.split()):
                kept["old"][field] += 1
            elif len(damaged_examples) < 5:
                damaged_examples.append({"field": field, "original": f"{old_label}: {value}", "old_output": old_out[:200]})
            if f"{new_label}: {value}" in new_out:
                kept["new"][field] += 1
        if str(row["subject_id"]) in new_out:
            raise AssertionError("subject_id leaked into formatted context")

    per_field = {
        f: {"values": total[f],
            "old_retained_pct": round(100 * kept["old"][f] / total[f], 1),
            "new_retained_pct": round(100 * kept["new"][f] / total[f], 1)}
        for f in COMPARED_FIELDS if total[f]
    }
    all_values = sum(total.values())
    old_rate = 100 * (1 - sum(kept["old"].values()) / all_values)
    new_rate = 100 * (1 - sum(kept["new"].values()) / all_values)

    # --- 2. PII leakage ---
    leaks = defaultdict(lambda: [0, 0])  # type -> [leaked, trials]
    for pii_type, sentence, secret in PII_PROBES:
        for row in rows[: args.probe_records]:
            probe = dict(row)
            probe["comments"] = f"{clean(row['comments']) or ''} {sentence}".strip()
            out = format_record(probe, redactor)
            leaks[pii_type][1] += 1
            if secret in out:
                leaks[pii_type][0] += 1
    total_trials = sum(t for _, t in leaks.values())
    total_leaks = sum(l for l, _ in leaks.values())

    # --- Report ---
    print(f"\nRecords sampled: {len(rows)}   Structured values checked: {all_values}\n")
    print(f"{'Field':<14}{'Values':>8}{'Old kept %':>12}{'New kept %':>12}")
    for f, r in per_field.items():
        print(f"{f:<14}{r['values']:>8}{r['old_retained_pct']:>12}{r['new_retained_pct']:>12}")
    print(f"\nOver-redaction rate: old {old_rate:.1f}%  ->  new {new_rate:.1f}%")
    if damaged_examples:
        print("\nExamples of old-pipeline damage:")
        for ex in damaged_examples[:3]:
            print(f"  {ex['original']!r}  ->  …{ex['old_output'][:120]!r}")

    print(f"\nPII leak test ({total_trials} injections into free text):")
    for pii_type, (leaked, trials) in leaks.items():
        status = "LEAK" if leaked else "ok"
        print(f"  {pii_type:<13} {leaked}/{trials} leaked  {status}")
    print(f"Overall leak rate: {100 * total_leaks / total_trials:.1f}%   Patient ID leaks: 0\n")

    out_dir = ROOT / "evals" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "redaction_eval.json").write_text(json.dumps({
        "sample": len(rows), "structured_values": all_values,
        "over_redaction_pct": {"old": round(old_rate, 1), "new": round(new_rate, 1)},
        "per_field": per_field,
        "pii_leaks": {k: {"leaked": l, "trials": t} for k, (l, t) in leaks.items()},
        "pii_leak_rate_pct": round(100 * total_leaks / total_trials, 1),
    }, indent=2))


if __name__ == "__main__":
    main()
