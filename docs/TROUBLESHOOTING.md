# Troubleshooting Log

Issues hit while setting up the EC2 Postgres instance and running the baseline ingestion, with how each was diagnosed and fixed.

**Environment at time of writing (Oct 2026)**

| Item | Value |
|---|---|
| EC2 instance | `i-0814588b1a576ca55` (`test-FDE-Database`), us-east-2c, Ubuntu |
| Public IP (Elastic IP `fde-ehr-clinical`) | `3.138.58.92` |
| Security group | `sg-02ab405bd8175b097` (`fde-ehr-insight-clinical`) |
| Postgres | 14, database `ehr_db`, user `fde_admin` |
| Local | macOS, Python 3.12 via uv, SQLAlchemy 2.1, psycopg 3 |

`<MY_IP>` below means your current public IP: `curl https://checkip.amazonaws.com`.

---

## 1. SSH times out

**Symptom**
```
ssh: connect to host 3.134.110.138 port 22: Operation timed out
```

**Diagnosis**
- *Timed out* (not *refused* or *permission denied*) means packets are dropped somewhere in the network, so the key is not the issue.
- Security group already allowed TCP 22 from `<MY_IP>/32`.
- The instance's **Public IPv4 DNS** in the console was `ec2-3-138-58-92...`, so the actual IP was `3.138.58.92`. `3.134.110.138` was the old auto-assigned IP from before the Elastic IP was attached.

**Fix**: connect to the current IP:
```
ssh -i test-FDE-Database.pem ubuntu@3.138.58.92
```

**Lesson**: always read the IP off the instance's details page or the Elastic IP, not from old notes. Quick reachability test: `nc -vz -G 5 <ip> 22`.

---

## 2. SSH rejects the key: "UNPROTECTED PRIVATE KEY FILE"

**Symptom**
```
Permissions 0644 for 'test-FDE-Database.pem' are too open.
Load key "test-FDE-Database.pem": bad permissions
ubuntu@3.138.58.92: Permission denied (publickey).
```

**Cause**: SSH ignores private keys readable by other users. Downloaded `.pem` files often come as `0644`.

**Fix**
```
chmod 400 test-FDE-Database.pem
```

---

## 3. Unused Elastic IP

**Finding**: two Elastic IPs were allocated:
- `fde-ehr-clinical`, `3.138.58.92`: associated with the instance. **Keep.**
- `fde-elastic-ip`, `3.23.229.148`: not associated with anything.

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
DB_HOST=3.138.58.92
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

---

## 7. Postgres port 5432 unreachable from the Mac

**Symptom**: `nc -vz 3.138.58.92 5432` timed out even though the security group allowed 5432 from `<MY_IP>/32`.

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
ssh -i test-FDE-Database.pem -N -L 5433:localhost:5432 ubuntu@3.138.58.92
```
`.env` for tunnel mode: `DB_HOST=localhost`, `DB_PORT=5433`. Local port 5433 avoids clashing with any Postgres running on the Mac.

This is still the fallback whenever `<MY_IP>` changes (new network or ISP reassignment) and direct connections start timing out.

---

## 8. Small gotchas

| Situation | What was going on |
|---|---|
| Ctrl+C "doesn't stop" the terminal | It was an idle SSH session, not a running process. Use `exit` or Ctrl+D to log out. |
| `sudo` asks for a password | The command ran on the **Mac**, not the server. The EC2 `ubuntu` user has passwordless sudo. Check the prompt: `ubuntu@ip-172-31-42-107` is the server. |
| `sudo -u postgres psql`: *could not change directory to /home/ubuntu* | Harmless. Use `sudo -i -u postgres psql` to avoid it. |
| `DtypeWarning: Columns (20: order_subtype) have mixed types` | Harmless. Silence with `pd.read_csv(..., low_memory=False)` or an explicit `dtype`. |

---

## Changing networks / IP changed

When direct DB or SSH connections suddenly time out:

1. `curl https://checkip.amazonaws.com` to get the new `<MY_IP>`.
2. Update the security group inbound rules for 22 and 5432 to `<MY_IP>/32`.
3. Update `pg_hba.conf` if it is restricted to a specific IP, then `sudo systemctl restart postgresql`.
4. Or skip steps 2–3 for 5432 and use the SSH tunnel (section 7).

---

## Open items

- [ ] Restrict `pg_hba.conf` from `0.0.0.0/0` to `<MY_IP>/32` (the security group is currently the only restriction).
- [ ] Rotate the `fde_admin` password (it was exposed in a screenshot).
- [ ] Fix the AWS CLI credentials (section 4).
- [ ] Release the unused Elastic IP `3.23.229.148` if not done yet (section 3).
- [ ] `01_ingest_baseline_data.py` uses `if_exists='append'`, so re-running duplicates all 232,158 rows. Truncate first or switch to `'replace'`.
