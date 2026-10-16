"""Access-control and audit-trail checks against the real API and database.

Creates two temporary users (random passwords), verifies every allow/deny rule and the audit trail,
then deletes the users and their audit rows. One LLM call (~$0.0005).

Usage: uv run python evals/security_eval.py
"""
import logging
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # the API loads ./src/guardrails relative to the working directory

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
logging.getLogger("presidio-analyzer").setLevel(logging.ERROR)

from fastapi.testclient import TestClient
from sqlalchemy import text

from src.api.audit import apply_security_schema
from src.api.auth import hash_password
from src.api.db import get_db_engine
from src.api.main import app

apply_security_schema()
eng = get_db_engine()
pw_c, pw_a = secrets.token_urlsafe(12), secrets.token_urlsafe(12)
with eng.begin() as c:
    pats = [r[0] for r in c.execute(text("select distinct subject_id from patient_encounters where clinical_embedding is not null order by 1"))]
    c.execute(text("insert into app_users values ('zz_test_clin', :h, 'clinician') on conflict (username) do update set password_hash=excluded.password_hash"), {"h": hash_password(pw_c)})
    c.execute(text("insert into app_users values ('zz_test_audit', :h, 'auditor') on conflict (username) do update set password_hash=excluded.password_hash"), {"h": hash_password(pw_a)})
    c.execute(text("delete from patient_access where username='zz_test_clin'"))
    for p in pats[:2]: c.execute(text("insert into patient_access values ('zz_test_clin', :p)"), {"p": p})
assigned, unassigned = pats[0], pats[-1]
results = []
def check(name, cond): results.append((name, cond)); print(("PASS " if cond else "FAIL ") + name)
try:
    with TestClient(app) as cl:
        check("no token -> 401", cl.get("/api/v1/patients").status_code == 401)
        check("tampered token -> 401", cl.get("/api/v1/patients", headers={"Authorization": "Bearer abc.def.ghi"}).status_code == 401)
        check("wrong password -> 401", cl.post("/api/v1/auth/login", json={"username": "zz_test_clin", "password": "nope"}).status_code == 401)
        tc = cl.post("/api/v1/auth/login", json={"username": "zz_test_clin", "password": pw_c}).json()["access_token"]
        ta = cl.post("/api/v1/auth/login", json={"username": "zz_test_audit", "password": pw_a}).json()["access_token"]
        H = lambda t: {"Authorization": f"Bearer {t}", "X-Forwarded-For": "203.0.113.7"}
        plist = cl.get("/api/v1/patients", headers=H(tc)).json()["patients"]
        check(f"clinician sees only assigned patients ({len(plist)} of {len(pats)})", plist == [str(p) for p in pats[:2]])
        check("clinician: assigned summary -> 200", cl.get(f"/api/v1/patients/{assigned}/summary", headers=H(tc)).status_code == 200)
        check("clinician: unassigned summary -> 403", cl.get(f"/api/v1/patients/{unassigned}/summary", headers=H(tc)).status_code == 403)
        check("clinician: chat on unassigned -> 403", cl.post("/api/v1/chat", headers=H(tc), json={"patient_id": str(unassigned), "messages": [{"role": "user", "content": "meds?"}]}).status_code == 403)
        check("clinician: audit log -> 403", cl.get("/api/v1/audit", headers=H(tc)).status_code == 403)
        check("auditor: patients -> 403", cl.get("/api/v1/patients", headers=H(ta)).status_code == 403)
        check("auditor: chat -> 403", cl.post("/api/v1/chat", headers=H(ta), json={"patient_id": str(assigned), "messages": [{"role": "user", "content": "meds?"}]}).status_code == 403)
        r = cl.post("/api/v1/chat", headers=H(tc), json={"patient_id": str(assigned), "messages": [{"role": "user", "content": "What medications is this patient on? Dr. Emily Carter asked."}]})
        check("clinician: chat on assigned -> 200", r.status_code == 200)
        ev = cl.get("/api/v1/audit", headers=H(ta), params={"username": "zz_test_clin"}).json()["events"]
        acts = {(e["action"], e["outcome"]) for e in ev}
        check("audit has failed login", ("login", "failed") in acts)
        check("audit has denied access", ("view_summary", "denied") in acts and ("chat", "denied") in acts)
        chat_ev = next(e for e in ev if e["action"] == "chat" and e["outcome"] == "success")
        check(f"audit chat has record_ids {chat_ev['record_ids']}", bool(chat_ev["record_ids"]))
        check(f"audit question redacted: {chat_ev['question_redacted']!r}", "Emily Carter" not in chat_ev["question_redacted"])
        check(f"audit client ip = {chat_ev['client_ip']}", chat_ev["client_ip"] == "203.0.113.7")
        ev_a = cl.get("/api/v1/audit", headers=H(ta), params={"username": "zz_test_audit", "action": "view_audit"}).json()["events"]
        check("viewing the audit log is itself audited", len(ev_a) >= 1)
finally:
    with eng.begin() as c:
        c.execute(text("delete from audit_log where username in ('zz_test_clin','zz_test_audit')"))
        c.execute(text("delete from app_users where username in ('zz_test_clin','zz_test_audit')"))
    print(f"\n{sum(ok for _, ok in results)}/{len(results)} passed; test users and their audit rows removed")
    sys.exit(0 if all(ok for _, ok in results) else 1)
