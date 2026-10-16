# uvicorn src.api.main:app --reload
import asyncio
import os
import time
from typing import List

from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine, text
from pydantic import BaseModel

from nemoguardrails import RailsConfig, LLMRails
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer

from src.pii_redaction.presidio_service import ClinicalPIIRedactor

load_dotenv()

app = FastAPI(title="Zero-Trust Clinical RAG API")

# Global state to hold our heavy ML models so they only load once at startup
middleware = {}

# Start of the "bot refuse medical advice" message in src/guardrails/rails.co
GUARDRAIL_REFUSAL_PREFIX = "I am an enterprise EHR retrieval system"

@app.on_event("startup")
async def startup_event():
    print("⏳ Booting Enterprise AI Middlewares...")
    
    # 1. Load Presidio (spaCy)
    middleware["redactor"] = ClinicalPIIRedactor()
    
    # 2. Load NeMo Guardrails (ModernBERT + DeepSeek)
    config = RailsConfig.from_path("./src/guardrails")
    middleware["rails"] = LLMRails(config)
    
    # 3. Load the local embedding model matching your embedding script
    print("⏳ Loading local BioClinical ModernBERT embedding model...")
    middleware["embedder"] = SentenceTransformer('NeuML/bioclinical-modernbert-base-embeddings')
    
    print("✅ System Ready on port 8000.")


# --- DATABASE CONNECTION HELPER ---
def get_db_engine():
    db_user = os.getenv("DB_USER")
    db_pass = os.getenv("DB_PASSWORD")
    db_host = os.getenv("DB_HOST")
    db_port = os.getenv("DB_PORT")
    db_name = os.getenv("DB_NAME")
    
    if db_host:
        DB_URL = f"postgresql+psycopg://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"
    else:
        DB_URL = os.getenv("POSTGRES_URL", "postgresql+psycopg://postgres:password@localhost:5432/clinical_db")
    
    return create_engine(DB_URL)


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


# --- ENDPOINT 1: HEALTH CHECK (MOCK DB) ---
@app.post("/api/v1/clinical-query")
async def process_clinical_query(query: ClinicalQuery):
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
async def process_chat(request: ChatRequest):
    try:
        started = time.perf_counter()
        latest_question = request.messages[-1].content
        engine = get_db_engine()
        
        # 1. Embed the user's question into a 768-dim vector
        embedder = middleware["embedder"]
        query_vector = embedder.encode(latest_question).tolist()
        
        # 2. Native pgvector similarity search on patient_encounters table
        with engine.connect() as conn:
            query = text("""
                SELECT id, drug, dose_val_rx, dose_unit_rx, route, eventtype, test_name, comments, description
                FROM patient_encounters
                WHERE subject_id = :subject_id AND clinical_embedding IS NOT NULL
                ORDER BY clinical_embedding <=> CAST(:query_embedding AS vector)
                LIMIT 5;
            """)
            
            result = conn.execute(query, {
                "subject_id": int(request.patient_id),
                "query_embedding": str(query_vector)
            })
            rows = result.fetchall()
            
            if not rows:
                real_db_context = f"No historical records found for patient {request.patient_id}."
            else:
                context_lines = []
                for row in rows:
                    context_lines.append(
                        f"Record ID: {row.id}, Drug: {row.drug} ({row.dose_val_rx} {row.dose_unit_rx}), Route: {row.route}, "
                        f"Event: {row.eventtype}, Test: {row.test_name}, Comments: {row.comments}, Diagnosis: {row.description}"
                    )
                real_db_context = f"[Records for Patient ID: {request.patient_id}]\n" + "\n".join(context_lines)

        # 3. Redact the real context via Presidio
        redactor = middleware["redactor"]
        safe_context = redactor.redact_clinical_context(raw_text=real_db_context)
        
        # 4. Assemble Prompt
        augmented_prompt = f"Clinical Context:\n{safe_context}\n\nUser Question: {latest_question}"
        
        # 5. Format History for Guardrails
        nemo_history = [{"role": msg.role, "content": msg.content} for msg in request.messages[:-1]]
        nemo_history.append({"role": "user", "content": augmented_prompt})
        
        # 6. Route through Guardrails
        rails = middleware["rails"]
        response = await rails.generate_async(messages=nemo_history)
        llm_response = response['content']

        return {
            "status": "success",
            "llm_response": llm_response,
            # Pipeline trace for the UI: what was retrieved, what the LLM actually saw, and whether a rail fired
            "records_retrieved": len(rows),
            "redacted_context": safe_context.strip(),
            "guardrail_triggered": llm_response.strip().startswith(GUARDRAIL_REFUSAL_PREFIX),
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- ENDPOINT 3: FETCH UNIQUE PATIENTS (EMBEDDINGS ONLY) ---
@app.get("/api/v1/patients")
async def get_unique_patients():
    try:
        engine = get_db_engine()
        with engine.connect() as conn:
            # Only fetch patients who actually have generated embeddings
            query = text("""
                SELECT DISTINCT subject_id 
                FROM patient_encounters 
                WHERE subject_id IS NOT NULL AND clinical_embedding IS NOT NULL 
                ORDER BY subject_id;
            """)
            result = conn.execute(query)
            patients = [str(row[0]) for row in result]
            
            return {"patients": patients if patients else ["No embedded patients found"]}
            
    except Exception as e:
        return {"patients": [], "error": str(e)}

# --- ENDPOINT 4: PATIENT SNAPSHOT (UI SIDEBAR; NOT SENT TO THE LLM) ---
@app.get("/api/v1/patients/{patient_id}/summary")
async def get_patient_summary(patient_id: int):
    try:
        engine = get_db_engine()
        with engine.connect() as conn:
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- ENDPOINT 5: HEALTH CHECK (API + DATABASE) ---
@app.get("/api/v1/health")
async def health():
    db_ok = True
    try:
        with get_db_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    return {"api": "ok", "database": "ok" if db_ok else "unreachable", "models_loaded": bool(middleware)}
