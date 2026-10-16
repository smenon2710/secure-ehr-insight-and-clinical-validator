"""Builds the per-record context line sent to the LLM, with field-aware redaction.

Structured columns hold coded values from small vocabularies (route is one of SC/IV/PO/NG...,
at most ~12 distinct values per column in the embedded data), so they cannot carry identifiers.
Running Presidio over them did damage instead: "Route: SC" became "<PERSON>: <LOCATION>".
Only free-text columns go through Presidio. The patient's subject_id is never included.
"""
from typing import Mapping

STRUCTURED_FIELDS = [
    ("Record ID", "id"),
    ("Admission", "admission_type"),
    ("Care unit", "careunit"),
    ("Event", "eventtype"),
    ("Drug", "drug"),
    ("Strength", "prod_strength"),
    ("Dose", "dose"),  # combined from dose_val_rx + dose_unit_rx
    ("Route", "route"),
    ("Lab test", "test_name"),
    ("Diagnosis", "description"),
    ("Severity", "drg_severity"),
]
FREE_TEXT_FIELDS = [("Comments", "comments")]

MIMIC_DEID_MARKER = "___"


def _clean(value) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return "" if text == MIMIC_DEID_MARKER else text


def format_record(row: Mapping, redactor) -> str:
    values = dict(row)
    values["dose"] = " ".join(_clean(values.get(k)) for k in ("dose_val_rx", "dose_unit_rx")).strip()

    parts = []
    for label, key in STRUCTURED_FIELDS:
        value = _clean(values.get(key))
        if value:
            parts.append(f"{label}: {value}")
    for label, key in FREE_TEXT_FIELDS:
        value = _clean(values.get(key))
        if value:
            parts.append(f"{label}: {redactor.redact_clinical_context(value).strip()}")
    return " | ".join(parts)
