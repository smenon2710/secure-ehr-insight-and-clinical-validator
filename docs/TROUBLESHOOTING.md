# Troubleshooting Log

Issues hit while setting up the EC2 Postgres instance and running the baseline ingestion, with how each was diagnosed and fixed.

**Environment at time of writing (Oct 2026)**

| Item | Value |
|---|---|
| EC2 instance | `<DB_INSTANCE_ID>` (`test-FDE-Database`), us-east-2c, Ubuntu |
| Public IP (Elastic IP `fde-ehr-clinical`) | `<DB_PUBLIC_IP>` |
| Security group | `<DB_SECURITY_GROUP_ID>` (`fde-ehr-insight-clinical`) |
| Postgres | 14, database `ehr_db`, user `fde_admin` |
| Local | macOS, Python 3.12 via uv, SQLAlchemy 2.1, psycopg 3 |

`<MY_IP>` below means your current public IP: `curl https://checkip.amazonaws.com`. Other `<...>` values (`<DB_PUBLIC_IP>`, `<APP_PRIVATE_IP>`, instance and security group IDs) stand in for this deployment's real identifiers; find them in the EC2 console.

---

## 1. SSH times out

**Symptom**
```
ssh: connect to host <OLD_AUTO_IP> port 22: Operation timed out
```

**Diagnosis**
- *Timed out* (not *refused* or *permission denied*) means packets are dropped somewhere in the network, so the key is not the issue.
- Security group already allowed TCP 22 from `<MY_IP>/32`.
- The instance's **Public IPv4 DNS** in the console was `ec2-<DB_PUBLIC_IP>...`, so the actual IP was `<DB_PUBLIC_IP>`. `<OLD_AUTO_IP>` was the old auto-assigned IP from before the Elastic IP was attached.

**Fix**: connect to the current IP:
```
ssh -i test-FDE-Database.pem ubuntu@<DB_PUBLIC_IP>
```

**Lesson**: always read the IP off the instance's details page or the Elastic IP, not from old notes. Quick reachability test: `nc -vz -G 5 <ip> 22`.

---

## 2. SSH rejects the key: "UNPROTECTED PRIVATE KEY FILE"

**Symptom**
```
Permissions 0644 for 'test-FDE-Database.pem' are too open.
Load key "test-FDE-Database.pem": bad permissions
ubuntu@<DB_PUBLIC_IP>: Permission denied (publickey).
```

**Cause**: SSH ignores private keys readable by other users. Downloaded `.pem` files often come as `0644`.

**Fix**
```
chmod 400 test-FDE-Database.pem
```

---

## 3. Unused Elastic IP

**Finding**: two Elastic IPs were allocated:
- `fde-ehr-clinical`, `<DB_PUBLIC_IP>`: associated with the instance. **Keep.**
- `fde-elastic-ip`, `<UNUSED_EIP>`: not associated with anything.

**Action**: release the unassociated one (EC2 → Elastic IPs → Actions → Release). An unattached EIP costs about $3.60/month. Check first that no DNS record or config points at it.

---

## 4. AWS CLI `AuthFailure`

**Symptom**
```
An error occurred (AuthFailure) ... AWS was not able to validate the provided access credentials
```

**Cause**: the CLI uses a static access key in `~/.aws/credentials` that is invalid (deactivated, rotated, or from another account). `aws sso login` does not apply because no SSO profile is configured.

**Fix (not yet done)**: create a new access key in IAM → Users → Security credentials, then run `aws configure` and verify with `aws sts get-caller-identity`.

---

## 5. Ingest script: `ValueError: invalid literal for int() with base 10: 'None'`

**Symptom**: `create_engine(db_url)` failed while parsing the port.

**Cause**: `.env` contained only `DB_HOST`. The script builds the URL with `os.getenv(...)` for each part, and missing variables become the string `"None"`:
```
postgresql://None:None@host:None/None
```

**Fix**: define all five variables in `.env`:
```
DB_HOST=<DB_PUBLIC_IP>
DB_PORT=5432
DB_USER=fde_admin
DB_PASSWORD=<password>
DB_NAME=ehr_db
```

---

## 6. `ModuleNotFoundError: No module named 'psycopg'`

**Cause**: SQLAlchemy **2.1** uses **psycopg 3** (`psycopg`) by default for `postgresql://` URLs. The venv only had `psycopg2-binary`.

**Fix**
```
uv add "psycopg[binary]"
```
(The alternative is to keep psycopg2 and use a `postgresql+psycopg2://` URL.)

### Also: `No module named 'sentence_transformers'` (script 04)

**Cause**: packages were installed ad hoc as each script needed them, never recorded in `pyproject.toml`.

**Fix**: check every script's imports at once (`grep -hE '^\s*(import|from) ' scripts/*.py | sort -u`) and add whatever is missing:
```
uv add sentence-transformers tqdm nemoguardrails
```
The first attempts failed with `stream closed because of a broken pipe` on a different package each time (`multidict`, `cuda-bindings`). That points to a flaky network, not a bad package. Fewer parallel downloads and a longer timeout fixed it:
```
UV_HTTP_TIMEOUT=180 UV_CONCURRENT_DOWNLOADS=2 uv add ...
```

### Also: `No module named 'presidio_analyzer'` (`src/pii_redaction/presidio_service.py`)

**Fix**: `uv add presidio-analyzer presidio-anonymizer`

**Catch**: Presidio's `AnalyzerEngine()` needs the spaCy model `en_core_web_lg`. If the model is missing, Presidio tries to download it at runtime with `pip`, which a uv venv doesn't have. Add the model as a pinned dependency instead, with the version matching spaCy's major.minor (spaCy 3.8 → model 3.8.0):
```
uv add "en_core_web_lg @ https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl"
```

### Also: NeMo Guardrails + OpenRouter (script 06)

- **Base URL ignored**: NeMo 0.24's `openai` engine reads `OPENAI_API_KEY` from env but **not** `OPENAI_BASE_URL`, so requests go to api.openai.com. The script now sets `parameters["base_url"]` from `OPENAI_BASE_URL` for `engine: openai` models.
- **YAML indentation**: a `- type: embeddings` list item indented under the previous item makes `config.yml` invalid. All `- type:` entries must line up.
- **`Missing or empty 'choices' in response: ['id', 'error']`**: NeMo hides the provider error. Call OpenRouter directly to see it:
  ```
  curl -s https://openrouter.ai/api/v1/chat/completions -H "Authorization: Bearer $OPENAI_API_KEY" \
    -H "Content-Type: application/json" -d '{"model":"<model>","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'
  ```
  Here it was **429 `engine_overloaded`**: `deepseek/deepseek-chat` failed ~50% of calls upstream. Switched to `deepseek/deepseek-v3.2` (8/8 OK). `curl https://openrouter.ai/api/v1/key -H "Authorization: Bearer $OPENAI_API_KEY"` confirms the key and remaining budget.

### Also: valid queries got made-up answers ("please confirm patient consent")

**Cause**: `rails.co` defined the *ask about patient history* intent but no flow, and nothing fed database records to the LLM, so it improvised.

**Fix**
- `src/guardrails/config.py` overrides NeMo's built-in `retrieve_relevant_chunks` action. It runs a pgvector search over `patient_encounters`, redacts results with Presidio, and returns them as `relevant_chunks`, which NeMo injects into the prompt.
- Added `define flow answer patient history` in `rails.co`. **Don't** add a `define bot provide patient history` with example text: NeMo then returns that text verbatim instead of generating.
- Added `instructions:` in `config.yml`: answer only from records, cite Record IDs, no "most recent" (no timestamps), no thinking output.
- `temperature: 0` reduced hallucinated IDs/values. Answers can still occasionally mis-cite a Record ID, so verify against the DB.

---

## 7. Postgres port 5432 unreachable from the Mac

**Symptom**: `nc -vz <DB_PUBLIC_IP> 5432` timed out even though the security group allowed 5432 from `<MY_IP>/32`.

**Diagnosis** (run on the server)
```
sudo ufw status                 # inactive, so not the OS firewall
sudo ss -tlnp | grep 5432       # 127.0.0.1:5432, so listening on localhost only
grep -n listen_addresses /etc/postgresql/14/main/postgresql.conf
```
`listen_addresses = '*'` was already set in the file. **Postgres had not been restarted** after the edit, so it was still running with the old setting.

**Fix**
```
sudo systemctl restart postgresql
sudo ss -tlnp | grep 5432       # now 0.0.0.0:5432 and [::]:5432
```

**Lesson**: edits to `postgresql.conf` / `pg_hba.conf` do nothing until a restart (or `SELECT pg_reload_conf();` for settings that support reload). Always confirm with `ss -tlnp`.

### Workaround used before the fix: SSH tunnel

Port 22 worked, so Postgres can be reached through SSH without exposing 5432:
```
# terminal 1, leave running (prints nothing); Ctrl+C to close
ssh -i test-FDE-Database.pem -N -L 5433:localhost:5432 ubuntu@<DB_PUBLIC_IP>
```
`.env` for tunnel mode: `DB_HOST=localhost`, `DB_PORT=5433`. Local port 5433 avoids clashing with any Postgres running on the Mac.

This is still the fallback whenever `<MY_IP>` changes (new network or ISP reassignment) and direct connections start timing out.

---

## 7b. Deployed app (Docker on a second EC2) shows no patient IDs

**Setup**: app container on EC2 `<APP_PUBLIC_IP>` (private `<APP_PRIVATE_IP>`); Postgres on the DB EC2 (public `<DB_PUBLIC_IP>`, private `<DB_PRIVATE_IP>`), same VPC.

**Symptom**: Streamlit patient dropdown empty / "Database Connection Error". No visible error, because `/api/v1/patients` returns HTTP 200 with `{"patients": [], "error": "..."}` when the DB call fails.

**Diagnosis**
```
docker exec fde-project-2 curl -s localhost:8000/api/v1/patients   # shows the real DB error
nc -vz -w 5 <DB_PRIVATE_IP> 5432                                       # from the app EC2; timed out
```
Port 8000 isn't published, so `curl localhost:8000` on the host returns nothing; always go through `docker exec`.

**Three blockers, all networking**
1. **DB security group** allowed 5432 only from the home IP. Fix: inbound TCP 5432 with the **app instance's security group** as the source.
2. **`DB_HOST` was the public IP.** Traffic to a public IP goes out through the internet gateway and arrives from the app's *public* IP, so a security-group-sourced rule never matches. Fix: `DB_HOST=<DB_PRIVATE_IP>` (private IP).
3. **`pg_hba.conf`** didn't allow the VPC range. Fix: `host all all 172.31.0.0/16 scram-sha-256`, then `sudo systemctl restart postgresql`.

Then **recreate** the container. `docker restart` does not re-read `--env-file`:
```
docker rm -f fde-project-2
docker run -d --name fde-project-2 --restart unless-stopped --env-file .env -p 8501:8501 fde-project-2:latest
```

**Lesson**: EC2-to-EC2 inside a VPC uses private IPs and security-group references. The UI's "Database Connection Error" also appears when the API itself is down, so check the API before the database.

---

## 7c. Deploying the sign-in version (access control + audit log)

**Context**: from commit `b3aa43f` the app requires sign-in. The server's `.env` needs `JWT_SECRET`, and users must be created with `scripts/07_manage_users.py`. Full steps are in `docs/CHANGELOG.md`.

### `can't open file '/app/scripts/07_manage_users.py'`
**Symptom**: the script exists on the EC2 host (`ls scripts/` shows it), but `docker exec fde-project-2 python scripts/07_manage_users.py ...` can't find it.

**Cause**: files reach the container only when the image is **built**. Either `.dockerignore` excluded `scripts/`, or the container was still running the image from before `git pull`.

**Diagnose**
```
cat .dockerignore                       # must not list scripts
docker exec fde-project-2 ls /app/src/api   # new code has auth.py, audit.py, db.py
```
**Fix**: remove `scripts` from `.dockerignore` if present, then rebuild and **recreate** the container:
```
docker build -t fde-project-2:latest .
docker rm -f fde-project-2
docker run -d --name fde-project-2 --restart unless-stopped --env-file .env -p 8501:8501 fde-project-2:latest
docker exec fde-project-2 ls /app/scripts   # 07_manage_users.py is listed
```
**Lesson**: `git pull` changes files on the host only. Running code changes after `docker build` **and** `docker rm` + `docker run`.

Also type `docker exec ... --first-n 3` on one line. A line break made the shell run `--first-n 10` as a separate command.

### Can't sign in although the password is "right"
**Symptom**: "Username or password is incorrect" for a user created moments earlier. Another user signs in fine.

**Diagnose**: every sign-in is audited, so check the auditor view ("Needs review") or query it:
```sql
SELECT ts, username, outcome FROM audit_log WHERE action = 'login' ORDER BY ts DESC LIMIT 10;
SELECT username, role FROM app_users;
```
Here the user existed with the right role and patients, and the log showed repeated `failed` logins. So the stored password didn't match what was being typed.

**Cause**: the password prompt (`getpass`) shows nothing while typing, so a typo is saved silently. Backspace can also misbehave through `docker exec -it`. Both entries matched each other, so the script accepted them.

**Fix**: re-run `add` with the same username to reset the password. Patient assignments are rebuilt by the command. Type slowly, with no backspace (Ctrl+C and restart instead). Easiest from the Mac, which avoids the Docker terminal:
```
uv run python scripts/07_manage_users.py add dr_lee --role clinician --first-n 3
```
Users live in the shared Postgres, so an account created from the Mac works on the EC2 site immediately.

### Other notes
- Usernames are case-sensitive (`dr_lee` ≠ `Dr_Lee`).
- Sessions expire after 8 hours; the UI returns to sign-in with a message.
- Container exits with `JWT_SECRET is not set`: the secret isn't in `.env`, or the container wasn't recreated with `--env-file .env`.

---

## 8. Small gotchas

| Situation | What was going on |
|---|---|
| Ctrl+C "doesn't stop" the terminal | It was an idle SSH session, not a running process. Use `exit` or Ctrl+D to log out. |
| `sudo` asks for a password | The command ran on the **Mac**, not the server. The EC2 `ubuntu` user has passwordless sudo. Check the prompt: `ubuntu@ip-<DB_PRIVATE_IP>` is the server. |
| `sudo -u postgres psql`: *could not change directory to /home/ubuntu* | Harmless. Use `sudo -i -u postgres psql` to avoid it. |
| `DtypeWarning: Columns (20: order_subtype) have mixed types` | Harmless. Silence with `pd.read_csv(..., low_memory=False)` or an explicit `dtype`. |
| `uvicorn` fails with `No module named 'nemoguardrails'` even with the venv active | The shell ran the **global** `~/.pyenv/.../uvicorn`. zsh had cached its path before `uvicorn` was installed in `.venv`. Run `rehash`, or always use `uv run uvicorn ...` / `uv run streamlit ...`. |
| `[Errno 48] Address already in use` on port 8000 | A leftover `uvicorn --reload` watcher from an earlier failed start. Find it with `lsof -nP -iTCP:8000 -sTCP:LISTEN` and `kill <PID>`. Stop servers with Ctrl+C rather than closing the tab. |
| UI shows "Database Connection Error" | The UI shows this whenever the **API** is unreachable, not only when the database is down. Check the API terminal or `docker logs` first. |

---

## Changing networks / IP changed

When direct DB or SSH connections suddenly time out:

1. `curl https://checkip.amazonaws.com` to get the new `<MY_IP>`.
2. Update the security group inbound rules for 22 and 5432 to `<MY_IP>/32`.
3. Update `pg_hba.conf` if it is restricted to a specific IP, then `sudo systemctl restart postgresql`.
4. Or skip steps 2–3 for 5432 and use the SSH tunnel (section 7).

---

## Open items

- [x] Restrict `pg_hba.conf` from `0.0.0.0/0` to `<MY_IP>/32` plus the VPC range `172.31.0.0/16` used by the app server. Done 2026-10-17.
- [ ] Make `audit_log` append-only at the DB level (a writer role without UPDATE/DELETE).
- [x] Rotate the `fde_admin` password (it was exposed in a screenshot). Done 2026-10-17.
- [ ] Fix the AWS CLI credentials (section 4).
- [ ] Release the unused Elastic IP `<UNUSED_EIP>` if not done yet (section 3).
- [ ] `01_ingest_baseline_data.py` uses `if_exists='append'`, so re-running duplicates all 232,158 rows. Truncate first or switch to `'replace'`.
