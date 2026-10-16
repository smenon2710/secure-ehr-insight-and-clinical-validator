import os
import re

import pandas as pd
import requests
import streamlit as st

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")

SUGGESTED_QUESTIONS = [
    "What medications is this patient on?",
    "Summarize this patient's lab tests.",
    "What diagnoses are recorded for this patient?",
    "Should I increase the Furosemide dose?",  # demonstrates the medical-advice guardrail
]

# Models cite records in several styles: "Record IDs: 3, 13", "(IDs 3, 13)", "records 4425, and 4426"
CITATION_PATTERN = re.compile(
    r"\b(?:record\s*ids?|records?|ids?)\s*[:#]?\s*((?:\d+(?:\s*(?:,\s*and|,\s*&|,|and|&)\s*)?)+)", re.IGNORECASE
)

st.set_page_config(page_title="Clinical Record Assistant", page_icon="🩺", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;500;600;700&display=swap');

:root {
  --paper: #F4F7F6; --panel: #E8EEEC; --ink: #16233F; --muted: #5B6B7F;
  --green: #0E6B57; --green-soft: #DCEFE9; --amber: #A35F00; --amber-soft: #FBEFD9;
  --red: #B42318; --red-soft: #FDE7E5; --rule: #CFD9D6;
}
html, body, [class*="css"], .stMarkdown, .stButton button, input, textarea, select {
  font-family: 'Public Sans', system-ui, sans-serif !important;
  font-feature-settings: "tnum" 1;
}
.block-container { padding-top: 2.2rem; max-width: 980px; }
[data-testid="stMetricValue"] { font-family: 'Public Sans', system-ui, sans-serif !important;
  font-size: 1.7rem; font-weight: 600; color: var(--ink); }
[data-testid="stMetricLabel"] p { font-size: 0.85rem; color: var(--muted); }
[data-testid="stMetricValue"] *, [data-baseweb="select"] *, [data-testid="stChatInput"] textarea,
[data-testid="stTextInput"] input { font-family: 'Public Sans', system-ui, sans-serif !important; }

.page-title { font-size: 1.75rem; font-weight: 700; color: var(--ink); line-height: 1.2; margin: 0; }
.page-meta { color: var(--muted); font-size: 0.95rem; margin: 0.25rem 0 1.2rem; }
.status { font-size: 0.82rem; color: var(--muted); }
.status b { font-weight: 600; }
.status .ok { color: var(--green); }
.status .bad { color: var(--red); }

.evidence { margin-top: 0.6rem; display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: center; }
.evidence-label { font-size: 0.82rem; color: var(--muted); margin-right: 0.2rem; }
.chip { font-size: 0.8rem; font-weight: 600; padding: 0.12rem 0.55rem; border-radius: 4px;
        border: 1px solid transparent; }
.chip-verified { background: var(--green-soft); color: var(--green); border-color: #B5DDD1; }
.chip-unverified { background: var(--amber-soft); color: var(--amber); border-color: #EDCF9C; }
.trace { font-size: 0.8rem; color: var(--muted); margin-top: 0.35rem; }

.blocked { border-left: 3px solid var(--amber); background: var(--amber-soft); padding: 0.7rem 0.9rem;
           border-radius: 0 6px 6px 0; color: var(--ink); }
.blocked-title { font-weight: 700; color: var(--amber); margin-bottom: 0.2rem; }

.note { font-size: 0.78rem; color: var(--muted); margin-top: 0.5rem; }
.sidebar-name { font-weight: 700; color: var(--ink); font-size: 1.02rem; margin-bottom: 0; }
.sidebar-role { color: var(--muted); font-size: 0.85rem; margin-top: 0.1rem; }
.sidebar-heading { font-weight: 600; color: var(--ink); font-size: 0.9rem; margin: 1rem 0 0.35rem; }
.drug { display: inline-block; font-size: 0.8rem; background: var(--panel); color: var(--ink);
        padding: 0.1rem 0.5rem; border-radius: 4px; margin: 0 0.3rem 0.3rem 0; }

.signin-title { font-size: 1.6rem; font-weight: 700; color: var(--ink); margin-bottom: 0.2rem; }
.signin-copy { color: var(--muted); margin-bottom: 1.2rem; }
</style>
""", unsafe_allow_html=True)


# --- API CLIENT ---
class SessionExpired(Exception):
    pass


def browser_ip():
    try:
        return st.context.ip_address
    except Exception:
        return None


def api(method: str, path: str, token: str = None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    ip = browser_ip()
    if ip:
        headers["X-Forwarded-For"] = ip
    response = requests.request(method, f"{API_BASE_URL}{path}", headers=headers, timeout=180, **kwargs)
    if response.status_code == 401 and token:
        raise SessionExpired()
    return response


@st.cache_data(ttl=30, show_spinner=False)
def fetch_health():
    try:
        return api("GET", "/api/v1/health").json()
    except requests.exceptions.RequestException:
        return {"api": "down", "database": "unknown"}


@st.cache_data(ttl=300, show_spinner=False)
def fetch_patients(token: str):
    return api("GET", "/api/v1/patients", token).json()


@st.cache_data(ttl=300, show_spinner=False)
def fetch_summary(token: str, patient_id: str):
    response = api("GET", f"/api/v1/patients/{patient_id}/summary", token)
    return response.json() if response.ok else None


def sign_out(message: str = None):
    for key in ("token", "user", "chats"):
        st.session_state.pop(key, None)
    st.cache_data.clear()
    if message:
        st.session_state["signin_message"] = message
    st.rerun()


# --- EVIDENCE HELPERS ---
def parse_context(context: str) -> pd.DataFrame:
    """Turn the redacted context ("Record ID: 23 | Drug: X | ...") into a table."""
    rows = []
    for line in context.splitlines():
        if not line.startswith("Record ID:"):
            continue
        fields = dict(part.split(": ", 1) for part in line.split(" | ") if ": " in part)
        rows.append(fields)
    return pd.DataFrame(rows)


def cited_ids(answer: str) -> list:
    ids = []
    for segment in CITATION_PATTERN.findall(answer):
        for n in re.findall(r"\d+", segment):
            if n not in ids:
                ids.append(n)
    return ids


def render_answer(reply: str, meta: dict):
    if meta.get("error"):
        st.error(reply)
        return

    if meta.get("guardrail_triggered"):
        st.markdown(
            f'<div class="blocked"><div class="blocked-title">Request not answered</div>{reply}</div>',
            unsafe_allow_html=True,
        )
        st.markdown('<div class="trace">Stopped by the medical-advice guardrail. Nothing was generated.</div>',
                    unsafe_allow_html=True)
        return

    st.markdown(reply)
    records = parse_context(meta.get("redacted_context", ""))
    retrieved = set(records["Record ID"]) if not records.empty else set()
    citations = cited_ids(reply)

    if citations:
        chips = "".join(
            f'<span class="chip chip-verified" title="Retrieved for this answer">Record {c}</span>' if c in retrieved
            else f'<span class="chip chip-unverified" title="Not among the retrieved records">Record {c}, unverified</span>'
            for c in citations
        )
        st.markdown(f'<div class="evidence"><span class="evidence-label">Evidence</span>{chips}</div>',
                    unsafe_allow_html=True)
        if any(c not in retrieved for c in citations):
            st.markdown('<div class="trace">An unverified record was cited. Check it against the chart before relying on it.</div>',
                        unsafe_allow_html=True)

    latency = f"{meta['latency_ms'] / 1000:.1f}s" if meta.get("latency_ms") is not None else ""
    st.markdown(f'<div class="trace">{len(records)} records retrieved for this patient, identifiers removed before '
                f'the model saw them. {latency}</div>', unsafe_allow_html=True)

    if not records.empty:
        with st.expander(f"Records the assistant saw ({len(records)})"):
            def highlight(row):
                style = "background-color: #DCEFE9; font-weight: 600" if row.get("Record ID") in citations else ""
                return [style] * len(row)
            st.dataframe(records.style.apply(highlight, axis=1), hide_index=True, use_container_width=True)

    st.markdown('<div class="note">Summarizes the patient record. Not a diagnosis or treatment recommendation.</div>',
                unsafe_allow_html=True)


# --- SIGN IN ---
def signin_view():
    _, center, _ = st.columns([1, 1.3, 1])
    with center:
        st.markdown('<div style="height: 8vh"></div>', unsafe_allow_html=True)
        st.markdown('<div class="signin-title">Clinical Record Assistant</div>', unsafe_allow_html=True)
        st.markdown('<div class="signin-copy">Ask questions about patients on your care team. '
                    'Answers come only from their records, and every request is logged.</div>',
                    unsafe_allow_html=True)
        if msg := st.session_state.pop("signin_message", None):
            st.info(msg)
        with st.form("signin"):
            username = st.text_input("Username", autocomplete="username")
            password = st.text_input("Password", type="password", autocomplete="current-password")
            submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)
        if submitted:
            try:
                response = api("POST", "/api/v1/auth/login", json={"username": username, "password": password})
            except requests.exceptions.RequestException:
                st.error("The service isn't reachable. Check that the API is running, then try again.")
                return
            if response.ok:
                data = response.json()
                st.session_state.token = data["access_token"]
                st.session_state.user = {"username": data["username"], "role": data["role"]}
                st.rerun()
            else:
                st.error("Username or password is incorrect.")


def sidebar_identity():
    user = st.session_state.user
    role_label = {"clinician": "Clinician", "auditor": "Compliance auditor"}[user["role"]]
    st.sidebar.markdown(f'<div class="sidebar-name">{user["username"]}</div>'
                        f'<div class="sidebar-role">{role_label}</div>', unsafe_allow_html=True)
    if st.sidebar.button("Sign out", use_container_width=True):
        sign_out()


def status_line():
    health = fetch_health()
    api_ok, db_ok = health.get("api") == "ok", health.get("database") == "ok"
    return (f'<span class="status"><b class="{"ok" if api_ok else "bad"}">●</b> Service '
            f'{"online" if api_ok else "offline"} &nbsp; <b class="{"ok" if db_ok else "bad"}">●</b> Records database '
            f'{"connected" if db_ok else "unreachable"}</span>')


# --- CLINICIAN ---
def clinician_view(token: str):
    data = fetch_patients(token)
    patients = data.get("patients", [])

    with st.sidebar:
        sidebar_identity()
        st.divider()
        if not patients:
            st.warning("No patients are assigned to you yet. Ask an administrator to add you to a care team.")
            st.stop()
        patient_id = st.selectbox(f"Patient ({len(patients)} on your care team)", patients)

        summary = fetch_summary(token, patient_id)
        if summary:
            c1, c2 = st.columns(2)
            c1.metric("Age", summary.get("age") or "—")
            c2.metric("Sex", summary.get("gender") or "—")
            c1.metric("Admissions", summary.get("admissions") or 0)
            c2.metric("Records", summary.get("embedded_records") or 0)
            if summary.get("top_drugs"):
                st.markdown('<div class="sidebar-heading">Most frequent medications</div>', unsafe_allow_html=True)
                st.markdown("".join(f'<span class="drug">{d}</span>' for d in summary["top_drugs"]),
                            unsafe_allow_html=True)
            if summary.get("diagnoses"):
                st.markdown('<div class="sidebar-heading">Recorded diagnoses</div>', unsafe_allow_html=True)
                for d in summary["diagnoses"]:
                    st.caption(d.capitalize())

        st.divider()
        if st.button("Start a new conversation", use_container_width=True):
            st.session_state.chats[patient_id] = []
            st.rerun()
        with st.expander("How answers are produced"):
            st.markdown(
                "- Only this patient's records are searched.\n"
                "- Names, dates and other identifiers are removed before the model sees anything.\n"
                "- Requests for diagnoses or prescribing advice are declined.\n"
                "- Every request is written to the audit log."
            )

    # Header
    head, status = st.columns([3, 2])
    head.markdown(f'<div class="page-title">Patient {patient_id}</div>', unsafe_allow_html=True)
    status.markdown(f'<div style="text-align:right; padding-top:0.55rem">{status_line()}</div>',
                    unsafe_allow_html=True)
    if summary:
        sex = {"F": "female", "M": "male"}.get(summary.get("gender"), "sex not recorded")
        st.markdown(f'<div class="page-meta">{summary.get("age", "Age unknown")}, {sex}, '
                    f'{summary.get("admissions", 0)} admissions on record</div>', unsafe_allow_html=True)

    # Conversation (one per patient, so history never crosses patients)
    st.session_state.setdefault("chats", {})
    history = st.session_state.chats.setdefault(patient_id, [])

    for msg in history:
        with st.chat_message(msg["role"], avatar="🧑‍⚕️" if msg["role"] == "user" else "🩺"):
            if msg["role"] == "assistant":
                render_answer(msg["content"], msg.get("meta", {}))
            else:
                st.markdown(msg["content"])

    suggested = None
    if not history:
        st.markdown("Ask about medications, lab results, diagnoses or admissions. For example:")
        cols = st.columns(2)
        for i, question in enumerate(SUGGESTED_QUESTIONS):
            if cols[i % 2].button(question, key=f"suggest-{i}", use_container_width=True):
                suggested = question

    prompt = st.chat_input(f"Ask about patient {patient_id}") or suggested
    if prompt:
        history.append({"role": "user", "content": prompt})
        with st.chat_message("user", avatar="🧑‍⚕️"):
            st.markdown(prompt)
        with st.chat_message("assistant", avatar="🩺"):
            with st.spinner("Searching this patient's records…"):
                try:
                    response = api("POST", "/api/v1/chat", token, json={
                        "patient_id": patient_id,
                        "messages": [{"role": m["role"], "content": m["content"]} for m in history],
                    })
                    if response.status_code == 403:
                        result = {"llm_response": "You are not assigned to this patient.", "error": True}
                    else:
                        response.raise_for_status()
                        result = response.json()
                except SessionExpired:
                    raise
                except requests.exceptions.RequestException as e:
                    result = {"llm_response": f"The request failed: {e}", "error": True}
            reply = result.get("llm_response") or "No answer was returned."
            meta = {k: v for k, v in result.items() if k != "llm_response"}
            render_answer(reply, meta)
        history.append({"role": "assistant", "content": reply, "meta": meta})
        if suggested:
            st.rerun()


# --- AUDITOR ---
ACTION_LABELS = {"login": "Sign-in", "list_patients": "Listed patients", "view_summary": "Viewed snapshot",
                 "chat": "Asked a question", "view_audit": "Viewed audit log"}


def auditor_view(token: str):
    with st.sidebar:
        sidebar_identity()
        st.divider()
        st.markdown('<div class="sidebar-heading">Filter events</div>', unsafe_allow_html=True)
        username = st.text_input("User", placeholder="Any user")
        patient = st.text_input("Patient ID", placeholder="Any patient")
        action = st.selectbox("Action", ["Any"] + list(ACTION_LABELS), format_func=lambda a: ACTION_LABELS.get(a, a))
        outcome = st.selectbox("Outcome", ["Any", "success", "denied", "failed", "error"],
                               format_func=lambda o: o.capitalize())
        limit = st.slider("Events to load", 50, 1000, 300, step=50)

    head, status = st.columns([3, 2])
    head.markdown('<div class="page-title">Access audit log</div>', unsafe_allow_html=True)
    status.markdown(f'<div style="text-align:right; padding-top:0.55rem">{status_line()}</div>',
                    unsafe_allow_html=True)
    st.markdown('<div class="page-meta">Every sign-in, patient lookup and question, with what was accessed. '
                'Questions are stored with identifiers removed.</div>', unsafe_allow_html=True)

    params = {"limit": limit}
    if username:
        params["username"] = username.strip()
    if patient.strip().isdigit():
        params["patient_id"] = int(patient)
    if action != "Any":
        params["action"] = action
    if outcome != "Any":
        params["outcome"] = outcome

    events = api("GET", "/api/v1/audit", token, params=params).json().get("events", [])
    if not events:
        st.info("No events match these filters.")
        return

    df = pd.DataFrame(events)
    df["ts"] = pd.to_datetime(df["ts"])

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Events shown", len(df))
    m2.metric("Denied access attempts", int((df["outcome"] == "denied").sum()))
    m3.metric("Failed sign-ins", int(((df["action"] == "login") & (df["outcome"] == "failed")).sum()))
    m4.metric("Guardrail blocks", int(df["guardrail_triggered"].fillna(False).astype(bool).sum()))

    denied = df[df["outcome"].isin(["denied", "failed"])]
    if not denied.empty:
        st.markdown('<div class="sidebar-heading">Needs review</div>', unsafe_allow_html=True)
        st.dataframe(
            pd.DataFrame({
                "Time": denied["ts"],
                "User": denied["username"].fillna(""),
                "Action": denied["action"].map(lambda a: ACTION_LABELS.get(a, a)),
                "Outcome": denied["outcome"].str.capitalize(),
                "Patient": denied["patient_id"].map(lambda p: "" if pd.isna(p) else str(int(p))),
                "Reason": denied["detail"].fillna(""),
                "IP address": denied["client_ip"].fillna(""),
            }),
            hide_index=True, use_container_width=True,
            column_config={"Time": st.column_config.DatetimeColumn(format="MMM D, HH:mm:ss")},
        )

    st.markdown('<div class="sidebar-heading">All events</div>', unsafe_allow_html=True)
    table = pd.DataFrame({
        "Time": df["ts"],
        "User": df["username"].fillna(""),
        "Action": df["action"].map(lambda a: ACTION_LABELS.get(a, a)),
        "Outcome": df["outcome"].str.capitalize(),
        "Patient": df["patient_id"].map(lambda p: "" if pd.isna(p) else str(int(p))),
        "Question (identifiers removed)": df["question_redacted"].fillna(""),
        "Guardrail": df["guardrail_triggered"].map({True: "Blocked", False: ""}).fillna(""),
        "Records accessed": df["record_ids"].map(lambda ids: ", ".join(map(str, ids)) if isinstance(ids, list) else ""),
        "Role": df["role"].fillna(""),
        "Time taken (ms)": df["latency_ms"].map(lambda v: "" if pd.isna(v) else f"{int(v):,}"),
    })
    st.dataframe(
        table, hide_index=True, use_container_width=True, height=460,
        column_config={"Time": st.column_config.DatetimeColumn(format="MMM D, HH:mm:ss"),
                       "Patient": st.column_config.TextColumn(width="small"),
                       "Question (identifiers removed)": st.column_config.TextColumn(width="large")},
    )
    st.download_button("Download these events as CSV", table.to_csv(index=False), "audit_log.csv", "text/csv")


# --- ROUTER ---
if "token" not in st.session_state:
    signin_view()
else:
    try:
        if st.session_state.user["role"] == "clinician":
            clinician_view(st.session_state.token)
        else:
            auditor_view(st.session_state.token)
    except SessionExpired:
        sign_out("Your session ended. Sign in again to continue.")
