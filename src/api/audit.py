import logging
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import text

from src.api.db import get_db_engine

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "database" / "security_schema.sql"


def apply_security_schema():
    with get_db_engine().begin() as conn:
        conn.execute(text(SCHEMA_PATH.read_text()))


def record(action: str, outcome: str, *, username: Optional[str] = None, role: Optional[str] = None,
           patient_id: Optional[int] = None, record_ids: Optional[Iterable[int]] = None,
           question_redacted: Optional[str] = None, guardrail_triggered: Optional[bool] = None,
           latency_ms: Optional[int] = None, client_ip: Optional[str] = None, detail: Optional[str] = None):
    """Append one audit event. Never raises: a logging failure must not take down a clinical request."""
    try:
        with get_db_engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO audit_log (username, role, action, outcome, patient_id, record_ids, question_redacted,
                                       guardrail_triggered, latency_ms, client_ip, detail)
                VALUES (:username, :role, :action, :outcome, :patient_id, :record_ids, :question_redacted,
                        :guardrail_triggered, :latency_ms, :client_ip, :detail)
            """), {
                "username": username, "role": role, "action": action, "outcome": outcome,
                "patient_id": patient_id, "record_ids": list(record_ids) if record_ids is not None else None,
                "question_redacted": question_redacted, "guardrail_triggered": guardrail_triggered,
                "latency_ms": latency_ms, "client_ip": client_ip, "detail": detail,
            })
    except Exception:
        log.exception("Failed to write audit event %s/%s", action, outcome)
