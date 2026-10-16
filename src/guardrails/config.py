import logging
import os
import sys
from pathlib import Path

from nemoguardrails import LLMRails
from nemoguardrails.actions import action
from nemoguardrails.actions.actions import ActionResult
from sentence_transformers import SentenceTransformer
from sqlalchemy import create_engine, text

# Make `src.*` importable when NeMo loads this file from src/guardrails/
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.pii_redaction.presidio_service import ClinicalPIIRedactor
from src.pii_redaction.record_formatter import format_record

TOP_K = 5

# Presidio logs a warning for every non-English recognizer it skips
logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)

_engine = None
_model = None
_redactor = None


def _load():
    global _engine, _model, _redactor
    if _engine is None:
        db_url = f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
        _engine = create_engine(db_url)
        _model = SentenceTransformer('NeuML/bioclinical-modernbert-base-embeddings')
        _redactor = ClinicalPIIRedactor()


@action(is_system_action=True)
async def retrieve_relevant_chunks(context: dict = None):
    """Overrides NeMo's built-in retrieval: pgvector search over patient_encounters,
    with Presidio redaction before anything reaches the LLM prompt."""
    user_message = (context or {}).get("last_user_message")
    # The API (src/api/main.py) already did patient-scoped retrieval + redaction and put it in the
    # prompt; a global search here would mix in other patients' records.
    if not user_message or user_message.startswith("Clinical Context:"):
        return ActionResult(return_value="", context_updates={"relevant_chunks": ""})

    _load()
    query_vector = _model.encode(user_message).tolist()

    search_sql = text("""
        SELECT id, admission_type, careunit, eventtype, drug, prod_strength, dose_val_rx, dose_unit_rx,
               route, test_name, comments, description, drg_severity
        FROM patient_encounters
        WHERE clinical_embedding IS NOT NULL
        ORDER BY clinical_embedding <=> CAST(:query_vector AS vector(768))
        LIMIT :top_k
    """)
    with _engine.connect() as conn:
        rows = conn.execute(search_sql, {"query_vector": str(query_vector), "top_k": TOP_K}).mappings().fetchall()

    chunks = [format_record(row, _redactor) for row in rows]
    relevant_chunks = "\n".join(chunks)

    return ActionResult(
        return_value=relevant_chunks,
        context_updates={
            "relevant_chunks": relevant_chunks,
            "relevant_chunks_sep": chunks,
            "retrieved_for": user_message,
        },
    )


def init(app: LLMRails):
    # NeMo's openai engine reads OPENAI_API_KEY but not OPENAI_BASE_URL, so route to OpenRouter explicitly.
    # init() runs before NeMo creates its LLM clients, so this applies to every LLMRails user (API + scripts).
    base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE")
    for model in app.config.models:
        if model.engine == "openai" and base_url:
            model.parameters.setdefault("base_url", base_url)

    app.register_action(retrieve_relevant_chunks, "retrieve_relevant_chunks")
