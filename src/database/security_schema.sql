-- src/database/security_schema.sql
-- Access control and audit trail. Applied automatically at API startup (idempotent).

CREATE TABLE IF NOT EXISTS app_users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,                      -- bcrypt
    role          TEXT NOT NULL CHECK (role IN ('clinician', 'auditor')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Care-team assignments: a clinician can only see patients listed here
CREATE TABLE IF NOT EXISTS patient_access (
    username   TEXT   NOT NULL REFERENCES app_users (username) ON DELETE CASCADE,
    subject_id BIGINT NOT NULL,
    PRIMARY KEY (username, subject_id)
);

-- Who did what, to which patient, and what data was touched.
-- Questions are stored Presidio-redacted so the log does not become a second PHI store.
CREATE TABLE IF NOT EXISTS audit_log (
    id                  BIGSERIAL PRIMARY KEY,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    username            TEXT,
    role                TEXT,
    action              TEXT NOT NULL,   -- login, list_patients, view_summary, chat, view_audit
    outcome             TEXT NOT NULL,   -- success, denied, failed, error
    patient_id          BIGINT,
    record_ids          BIGINT[],
    question_redacted   TEXT,
    guardrail_triggered BOOLEAN,
    latency_ms          INT,
    client_ip           TEXT,
    detail              TEXT
);

CREATE INDEX IF NOT EXISTS audit_log_ts_idx ON audit_log (ts DESC);
CREATE INDEX IF NOT EXISTS audit_log_patient_idx ON audit_log (patient_id);
