# uvicorn src.api.main:app --reload
import time
from contextlib import asynccontextmanager
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status
from nemoguardrails import LLMRails, RailsConfig
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer
from sqlalchemy import text

from src.api import audit
from src.api.auth import User, authenticate, create_token, get_current_user, require_role
from src.api.db import get_db_engine
from src.pii_redaction.presidio_service import ClinicalPIIRedactor
from src.pii_redaction.record_formatter import format_record

load_dotenv()

# Global state to hold our heavy ML models so they only load once at startup
middleware = {}

# Start of the "bot refuse medical advice" message in src/guardrails/rails.co
GUARDRAIL_REFUSAL_PREFIX = "I am an enterprise EHR retrieval system"

# Coded columns whose values must survive redaction of the clinician's own questions
CLINICAL_VOCABULARY_COLUMNS = ("drug", "test_name", "route", "eventtype", "careunit", "admission_type")


def load_clinical_vocabulary() -> set:
    """Distinct coded values from the records, plus each drug's leading word ("Albuterol" from "Albuterol Inhaler")."""
    terms = set()
    with get_db_engine().connect() as conn:
        for column in CLINICAL_VOCABULARY_COLUMNS:
            values = [r[0] for r in conn.execute(text(f"SELECT DISTINCT {column} FROM patient_encounters "
                                                      f"WHERE {column} IS NOT NULL"))]
            terms.update(values)
            if column == "drug":
                terms.update(v.split()[0] for v in values if v.split())
    return terms


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("⏳ Booting Enterprise AI Middlewares...")

    # 0. Access-control and audit tables (idempotent)
    audit.apply_security_schema()

    # 1. Load Presidio (spaCy)
    middleware["redactor"] = ClinicalPIIRedactor()
    middleware["redactor"].allow_terms(load_clinical_vocabulary())

    # 2. Load NeMo Guardrails (ModernBERT + DeepSeek)
    config = RailsConfig.from_path("./src/guardrails")
    middleware["rails"] = LLMRails(config)

    # 3. Load the local embedding model matching your embedding script
    print("⏳ Loading local BioClinical ModernBERT embedding model...")
    middleware["embedder"] = SentenceTransformer('NeuML/bioclinical-modernbert-base-embeddings')

    print("✅ System Ready on port 8000.")
    yield


app = FastAPI(title="Zero-Trust Clinical RAG API", lifespan=lifespan)


# --- MODELS ---
class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    patient_id: str
    messages: List[ChatMessage]

class ClinicalQuery(BaseModel):
    patient_id: str
    prompt: str

class LoginRequest(BaseModel):
    username: str
    password: str


# --- HELPERS ---
def client_ip(request: Request) -> Optional[str]:
    # The Streamlit UI forwards the browser's IP; fall back to the direct caller
    forwarded = request.headers.get("x-forwarded-for")
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else None)


def authorize_patient(user: User, patient_id: int, action: str, request: Request):
    """Care-team check: a clinician may only touch patients assigned to them. Denials are audited."""
    if patient_id not in user.patient_ids:
        audit.record(action, "denied", username=user.username, role=user.role, patient_id=patient_id,
                     client_ip=client_ip(request), detail="patient not assigned to user")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You are not assigned to this patient")


# --- AUTH ---
@app.post("/api/v1/auth/login")
async def login(body: LoginRequest, request: Request):
    result = authenticate(body.username, body.password)
    if result is None:
        audit.record("login", "failed", username=body.username, client_ip=client_ip(request))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    username, role = result
    audit.record("login", "success", username=username, role=role, client_ip=client_ip(request))
    return {"access_token": create_token(username, role), "token_type": "bearer", "username": username, "role": role}


@app.get("/api/v1/auth/me")
async def me(user: User = Depends(get_current_user)):
    return {"username": user.username, "role": user.role, "assigned_patients": len(user.patient_ids)}


# --- ENDPOINT 1: MOCK CLINICAL QUERY (DEMO DATA, NO DB) ---
@app.post("/api/v1/clinical-query")
async def process_clinical_query(query: ClinicalQuery, user: User = Depends(require_role("clinician"))):
    try:
        raw_db_context = f"""
        Patient John Doe (ID: {query.patient_id}) was admitted on March 15th.
        Last recorded Furosemide dosage was 40mg IV.
        Attending physician: Dr. Gregory House, ID: 20043.
        """

        redactor = middleware["redactor"]
        safe_context = redactor.redact_clinical_context(raw_text=raw_db_context)

        augmented_prompt = f"Clinical Context:\n{safe_context}\n\nUser Question: {query.prompt}"

        rails = middleware["rails"]
        response = await rails.generate_async(messages=[{"role": "user", "content": augmented_prompt}])

        return {
            "status": "success",
            "redacted_context_used": safe_context.strip(),
            "llm_response": response['content']
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- ENDPOINT 2: PRODUCTION CHAT (NATIVE PGVECTOR DB) ---
@app.post("/api/v1/chat")
async def process_chat(chat: ChatRequest, request: Request, user: User = Depends(require_role("clinician"))):
    started = time.perf_counter()
    patient_id = int(chat.patient_id)
    authorize_patient(user, patient_id, "chat", request)

    latest_question = chat.messages[-1].content
    redactor = middleware["redactor"]
    # Identifiers the clinician types (names, dates, MRNs) never reach the LLM either. Assistant turns are
    # earlier model output, generated from already-redacted context.
    safe_messages = [
        {"role": msg.role, "content": redactor.redact_clinical_context(msg.content) if msg.role == "user" else msg.content}
        for msg in chat.messages
    ]
    question_redacted = safe_messages[-1]["content"]
    try:
        # 1. Embed the user's question into a 768-dim vector (local model, so the unredacted text is fine here)
        query_vector = middleware["embedder"].encode(latest_question).tolist()

        # 2. Native pgvector similarity search, scoped to the one authorized patient
        with get_db_engine().connect() as conn:
            rows = conn.execute(text("""
                SELECT id, admission_type, careunit, eventtype, drug, prod_strength, dose_val_rx, dose_unit_rx,
                       route, test_name, comments, description, drg_severity
                FROM patient_encounters
                WHERE subject_id = :subject_id AND clinical_embedding IS NOT NULL
                ORDER BY clinical_embedding <=> CAST(:query_embedding AS vector)
                LIMIT 5;
            """), {"subject_id": patient_id, "query_embedding": str(query_vector)}).mappings().fetchall()

        # 3. Format records with field-aware redaction (Presidio on free text only; no patient ID sent to the LLM)
        if not rows:
            safe_context = "No historical records found for the selected patient."
        else:
            safe_context = "[Records for the selected patient]\n" + "\n".join(
                format_record(row, redactor) for row in rows
            )

        # 4. Assemble Prompt
        augmented_prompt = f"Clinical Context:\n{safe_context}\n\nUser Question: {question_redacted}"

        # 5. Format History for Guardrails
        nemo_history = safe_messages[:-1]
        nemo_history.append({"role": "user", "content": augmented_prompt})

        # 6. Route through Guardrails
        response = await middleware["rails"].generate_async(messages=nemo_history)
        llm_response = response['content']
        guardrail_triggered = llm_response.strip().startswith(GUARDRAIL_REFUSAL_PREFIX)
        latency_ms = round((time.perf_counter() - started) * 1000)
    except Exception as e:
        audit.record("chat", "error", username=user.username, role=user.role, patient_id=patient_id,
                     question_redacted=question_redacted, client_ip=client_ip(request), detail=type(e).__name__)
        raise HTTPException(status_code=500, detail=str(e))

    audit.record("chat", "success", username=user.username, role=user.role, patient_id=patient_id,
                 record_ids=[row["id"] for row in rows], question_redacted=question_redacted,
                 guardrail_triggered=guardrail_triggered, latency_ms=latency_ms, client_ip=client_ip(request))

    return {
        "status": "success",
        "llm_response": llm_response,
        # Pipeline trace for the UI: what was retrieved, what the LLM actually saw, and whether a rail fired
        "records_retrieved": len(rows),
        "redacted_context": safe_context.strip(),
        "guardrail_triggered": guardrail_triggered,
        "latency_ms": latency_ms,
    }


# --- ENDPOINT 3: PATIENTS ASSIGNED TO THE CLINICIAN (EMBEDDINGS ONLY) ---
@app.get("/api/v1/patients")
async def get_unique_patients(request: Request, user: User = Depends(require_role("clinician"))):
    try:
        with get_db_engine().connect() as conn:
            # Only patients who have embeddings AND are on this clinician's care team
            rows = conn.execute(text("""
                SELECT DISTINCT pe.subject_id
                FROM patient_encounters pe
                JOIN patient_access pa ON pa.subject_id = pe.subject_id AND pa.username = :username
                WHERE pe.clinical_embedding IS NOT NULL
                ORDER BY pe.subject_id;
            """), {"username": user.username})
            patients = [str(row[0]) for row in rows]
    except Exception as e:
        return {"patients": [], "error": str(e)}

    audit.record("list_patients", "success", username=user.username, role=user.role, client_ip=client_ip(request),
                 detail=f"{len(patients)} patients")
    return {"patients": patients}


# --- ENDPOINT 4: PATIENT SNAPSHOT (UI SIDEBAR; NOT SENT TO THE LLM) ---
@app.get("/api/v1/patients/{patient_id}/summary")
async def get_patient_summary(patient_id: int, request: Request, user: User = Depends(require_role("clinician"))):
    authorize_patient(user, patient_id, "view_summary", request)
    try:
        with get_db_engine().connect() as conn:
            overview = conn.execute(text("""
                SELECT MAX(anchor_age) AS age, MAX(gender) AS gender,
                       COUNT(*) AS encounters, COUNT(clinical_embedding) AS embedded,
                       COUNT(DISTINCT hadm_id) AS admissions
                FROM patient_encounters
                WHERE subject_id = :subject_id
            """), {"subject_id": patient_id}).mappings().one()

            top_drugs = conn.execute(text("""
                SELECT drug, COUNT(*) AS n
                FROM patient_encounters
                WHERE subject_id = :subject_id AND drug IS NOT NULL
                GROUP BY drug ORDER BY n DESC LIMIT 5
            """), {"subject_id": patient_id}).mappings().fetchall()

            diagnoses = conn.execute(text("""
                SELECT description, COUNT(*) AS n
                FROM patient_encounters
                WHERE subject_id = :subject_id AND description IS NOT NULL
                GROUP BY description ORDER BY n DESC LIMIT 3
            """), {"subject_id": patient_id}).mappings().fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    audit.record("view_summary", "success", username=user.username, role=user.role, patient_id=patient_id,
                 client_ip=client_ip(request))
    return {
        "patient_id": str(patient_id),
        "age": overview["age"],
        "gender": overview["gender"],
        "encounters": overview["encounters"],
        "embedded_records": overview["embedded"],
        "admissions": overview["admissions"],
        "top_drugs": [row["drug"] for row in top_drugs],
        "diagnoses": [row["description"] for row in diagnoses],
    }


# --- ENDPOINT 5: HEALTH CHECK (API + DATABASE; NO AUTH, NO PATIENT DATA) ---
@app.get("/api/v1/health")
async def health():
    db_ok = True
    try:
        with get_db_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    return {"api": "ok", "database": "ok" if db_ok else "unreachable", "models_loaded": bool(middleware)}


# --- ENDPOINT 6: AUDIT LOG (AUDITORS ONLY) ---
@app.get("/api/v1/audit")
async def get_audit_log(request: Request, limit: int = 200, username: Optional[str] = None,
                        patient_id: Optional[int] = None, action: Optional[str] = None,
                        outcome: Optional[str] = None, user: User = Depends(require_role("auditor"))):
    filters, params = [], {"limit": min(max(limit, 1), 1000)}
    for column, value in (("username", username), ("patient_id", patient_id), ("action", action), ("outcome", outcome)):
        if value is not None:
            filters.append(f"{column} = :{column}")
            params[column] = value
    where = f"WHERE {' AND '.join(filters)}" if filters else ""

    with get_db_engine().connect() as conn:
        rows = conn.execute(text(f"""
            SELECT id, ts, username, role, action, outcome, patient_id, record_ids, question_redacted,
                   guardrail_triggered, latency_ms, client_ip, detail
            FROM audit_log {where}
            ORDER BY ts DESC LIMIT :limit
        """), params).mappings().fetchall()

    # Reading the audit log is itself an audited action
    audit.record("view_audit", "success", username=user.username, role=user.role, client_ip=client_ip(request),
                 detail=f"{len(rows)} events")
    return {"events": [{**row, "ts": row["ts"].isoformat()} for row in rows]}
