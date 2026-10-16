import os

import requests
import streamlit as st

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
API_CHAT_URL = f"{API_BASE_URL}/api/v1/chat"
API_PATIENTS_URL = f"{API_BASE_URL}/api/v1/patients"
API_HEALTH_URL = f"{API_BASE_URL}/api/v1/health"

DISCLAIMER = "AI-generated summary of the patient record. Not for diagnostic use."

SUGGESTED_QUESTIONS = [
    "What medications is this patient on?",
    "Summarize this patient's lab tests.",
    "What diagnoses are recorded for this patient?",
    "Should I increase the Furosemide dose?",  # demonstrates the medical-advice guardrail
]

st.set_page_config(page_title="Clinical EHR Assistant", page_icon="🩺", layout="wide")

st.markdown("""
<style>
.block-container { padding-top: 2rem; max-width: 1100px; }
.app-title { font-size: 1.9rem; font-weight: 700; margin-bottom: 0.1rem; }
.app-subtitle { color: var(--text-muted, #8b93a7); margin-bottom: 0.8rem; }
.pill { display: inline-block; padding: 0.15rem 0.6rem; margin: 0 0.35rem 0.35rem 0; border-radius: 999px;
        font-size: 0.78rem; font-weight: 600; border: 1px solid rgba(127,127,127,0.35); }
.pill-ok { background: rgba(34,197,94,0.12); color: #22c55e; border-color: rgba(34,197,94,0.35); }
.pill-bad { background: rgba(239,68,68,0.12); color: #ef4444; border-color: rgba(239,68,68,0.35); }
.pill-info { background: rgba(59,130,246,0.12); color: #3b82f6; border-color: rgba(59,130,246,0.35); }
.pill-warn { background: rgba(245,158,11,0.14); color: #f59e0b; border-color: rgba(245,158,11,0.4); }
.disclaimer { font-size: 0.78rem; color: #8b93a7; margin-top: 0.4rem; }
.section-label { font-size: 0.72rem; letter-spacing: 0.08em; text-transform: uppercase; color: #8b93a7;
                 margin: 0.9rem 0 0.3rem; font-weight: 600; }
</style>
""", unsafe_allow_html=True)


def pill(text: str, kind: str = "info") -> str:
    return f'<span class="pill pill-{kind}">{text}</span>'


# --- API CALLS ---
@st.cache_data(ttl=30)
def fetch_health():
    try:
        return requests.get(API_HEALTH_URL, timeout=5).json()
    except requests.exceptions.RequestException:
        return {"api": "down", "database": "unknown"}


@st.cache_data(ttl=300)  # Cache the list for 5 minutes to reduce database load
def fetch_patient_list():
    try:
        data = requests.get(API_PATIENTS_URL, timeout=30).json()
        return data.get("patients", []), data.get("error")
    except requests.exceptions.RequestException as e:
        return [], f"API unreachable: {e}"


@st.cache_data(ttl=300)
def fetch_patient_summary(patient_id: str):
    try:
        response = requests.get(f"{API_PATIENTS_URL}/{patient_id}/summary", timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException:
        return None


def ask(patient_id: str, history: list) -> dict:
    payload = {
        "patient_id": patient_id,
        "messages": [{"role": m["role"], "content": m["content"]} for m in history],
    }
    try:
        response = requests.post(API_CHAT_URL, json=payload, timeout=180)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"llm_response": f"Request failed: {e}", "error": True}


def render_trace(meta: dict):
    """Pipeline transparency: what was retrieved, what the LLM saw, and which rail fired."""
    if meta.get("error"):
        return
    badges = []
    if meta.get("guardrail_triggered"):
        badges.append(pill("🛡️ Blocked by NeMo Guardrail", "warn"))
    else:
        badges.append(pill(f"📄 {meta.get('records_retrieved', 0)} records retrieved", "info"))
    badges.append(pill("🔒 PII redacted (Presidio)", "ok"))
    if meta.get("latency_ms") is not None:
        badges.append(pill(f"⏱ {meta['latency_ms'] / 1000:.1f}s", "info"))
    st.markdown("".join(badges), unsafe_allow_html=True)

    if meta.get("redacted_context"):
        with st.expander("View redacted context sent to the LLM"):
            st.code(meta["redacted_context"], language=None, wrap_lines=True)


# --- SIDEBAR ---
health = fetch_health()
patients, patients_error = fetch_patient_list()

with st.sidebar:
    st.markdown("### 🩺 Clinical EHR Assistant")
    st.caption("Zero-trust retrieval over MIMIC-IV")

    if not patients:
        st.error(f"Could not load patients. {patients_error or ''}")
        st.stop()

    patient_id = st.selectbox("Patient", patients, help="Type to search. All retrieval is scoped to this patient.")

    summary = fetch_patient_summary(patient_id)
    if summary:
        st.markdown('<div class="section-label">Snapshot</div>', unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        c1.metric("Age", summary.get("age") or "—")
        c2.metric("Sex", summary.get("gender") or "—")
        c1.metric("Admissions", summary.get("admissions") or 0)
        c2.metric("Records", f"{summary.get('embedded_records', 0)}/{summary.get('encounters', 0)}",
                  help="Vectorized / total encounter records")

        if summary.get("top_drugs"):
            st.markdown('<div class="section-label">Frequent medications</div>', unsafe_allow_html=True)
            st.markdown("".join(pill(d, "info") for d in summary["top_drugs"]), unsafe_allow_html=True)
        if summary.get("diagnoses"):
            st.markdown('<div class="section-label">Recorded diagnoses</div>', unsafe_allow_html=True)
            for d in summary["diagnoses"]:
                st.caption(f"• {d}")

    st.divider()
    if st.button("New conversation", use_container_width=True):
        st.session_state.chats[patient_id] = []
        st.rerun()

    with st.expander("How it works"):
        st.markdown(
            "1. **Retrieve**: pgvector similarity search, scoped to the selected patient\n"
            "2. **Redact**: Microsoft Presidio removes names, dates, IDs and contact details\n"
            "3. **Guard**: NeMo Guardrails blocks diagnosis and prescribing requests\n"
            "4. **Generate**: the LLM answers only from the redacted records"
        )

# --- HEADER ---
st.markdown('<div class="app-title">Patient Record Assistant</div>', unsafe_allow_html=True)
st.markdown('<div class="app-subtitle">Ask questions about a patient\'s history. Answers are grounded in their records only.</div>',
            unsafe_allow_html=True)
api_ok = health.get("api") == "ok"
db_ok = health.get("database") == "ok"
st.markdown(
    pill("● API online" if api_ok else "● API offline", "ok" if api_ok else "bad")
    + pill("● Database connected" if db_ok else "● Database unreachable", "ok" if db_ok else "bad")
    + pill("Presidio PII redaction", "info")
    + pill("NeMo Guardrails", "info")
    + pill(f"Patient {patient_id}", "info"),
    unsafe_allow_html=True,
)

# --- CHAT STATE (one conversation per patient, so history never crosses patients) ---
if "chats" not in st.session_state:
    st.session_state.chats = {}
history = st.session_state.chats.setdefault(patient_id, [])

for msg in history:
    with st.chat_message(msg["role"], avatar="🧑‍⚕️" if msg["role"] == "user" else "🩺"):
        st.markdown(msg["content"])
        if msg["role"] == "assistant":
            render_trace(msg.get("meta", {}))
            st.markdown(f'<div class="disclaimer">⚠️ {DISCLAIMER}</div>', unsafe_allow_html=True)

# --- EMPTY STATE: SUGGESTED QUESTIONS ---
suggested = None
if not history:
    st.markdown('<div class="section-label">Try asking</div>', unsafe_allow_html=True)
    cols = st.columns(2)
    for i, question in enumerate(SUGGESTED_QUESTIONS):
        if cols[i % 2].button(question, key=f"suggest-{i}", use_container_width=True):
            suggested = question

# --- CHAT INPUT & API CALL ---
prompt = st.chat_input(f"Ask about patient {patient_id}'s history...") or suggested
if prompt:
    history.append({"role": "user", "content": prompt})
    with st.chat_message("user", avatar="🧑‍⚕️"):
        st.markdown(prompt)

    with st.chat_message("assistant", avatar="🩺"):
        with st.spinner("Retrieving records, redacting PII, checking guardrails..."):
            result = ask(patient_id, history)
        reply = result.get("llm_response") or "No response."
        meta = {k: v for k, v in result.items() if k != "llm_response"}
        st.markdown(reply)
        render_trace(meta)
        st.markdown(f'<div class="disclaimer">⚠️ {DISCLAIMER}</div>', unsafe_allow_html=True)

    history.append({"role": "assistant", "content": reply, "meta": meta})
    if suggested:
        st.rerun()
