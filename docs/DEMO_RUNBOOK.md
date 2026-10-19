# Demo runbook

To save AWS credits, both EC2 instances are **stopped** when no demo is planned. Stopped instances don't bill for compute. Their EBS volumes and the DB server's Elastic IP still bill a few dollars a month.

`<...>` placeholders follow the convention in `docs/TROUBLESHOOTING.md`. Take real values from the EC2 console.

## Bring it up (allow 10 minutes before the demo)

1. **Start the database first.** EC2 → Instances → `test-FDE-Database` → Instance state → **Start**. Wait for **Running** and **2/2 checks passed**.
   - PostgreSQL starts with the instance. Its Elastic IP (`fde-ehr-clinical`) and private IP don't change, so the app's `DB_HOST` still works.
2. **Then start the app server**, the other instance. The container (`fde-project-2`, `--restart unless-stopped`) comes back on its own.
   - The API reads the database at startup. If the app booted before the DB was ready, the container exits and Docker restarts it until the DB answers. That's expected.
3. **Get the app's public IP.** Open the app instance → Details → **Public IPv4 address**. It usually **changes on every start** because the app server has no Elastic IP. The demo URL is `http://<APP_PUBLIC_IP>:8501`.
4. **Check that it's healthy.** SSH in with `ssh -i <app-key>.pem ubuntu@<APP_PUBLIC_IP>`, then:
   ```
   docker ps                                                   # fde-project-2 is "Up", not "Restarting"
   docker exec fde-project-2 curl -s localhost:8000/api/v1/health
   # expect {"api":"ok","database":"ok","models_loaded":true}
   ```
   The models take a minute or two to load after the container starts, so retry if `models_loaded` is false.
5. **Rehearse in the browser.**
   - Open `http://<APP_PUBLIC_IP>:8501`.
   - Sign in as a clinician (e.g. `dr_lee`), pick a patient and ask one question.
   - Check that the answer shows evidence chips.
   - Try the suggested question that triggers the medical-advice guardrail.
   - Sign in as the auditor (`compliance`) and check that both requests appear in the audit log.

### If something doesn't work

| Symptom | Likely cause | Fix |
|---|---|---|
| Browser can't reach `:8501` | Using the old IP, or the security group limits 8501 to an IP that has since changed | Use the new public IP from step 3. If 8501 is limited to your IP, update the rule (`curl https://checkip.amazonaws.com`) |
| SSH times out | Port 22 is limited to your old home IP | Update the security group rule for 22 to `<MY_IP>/32` (TROUBLESHOOTING, "Changing networks") |
| `docker ps` shows "Restarting" | API can't reach the DB, or `JWT_SECRET` is missing | `docker logs --tail 50 fde-project-2`. Confirm the DB instance is running, then see TROUBLESHOOTING 7b |
| Health says `"database":"unreachable"` | DB instance not running, or PostgreSQL not up | On the DB server: `sudo systemctl status postgresql`, then `sudo systemctl start postgresql` if needed |
| "Username or password is incorrect" | Wrong or forgotten demo password | Reset it: `docker exec -it fde-project-2 python scripts/07_manage_users.py ...` (TROUBLESHOOTING 7c) |

## Shut it down after the demo

EC2 → Instances → select **both** → Instance state → **Stop**. Leave "Skip OS shutdown" **unchecked** so PostgreSQL shuts down cleanly.

Don't **terminate** either instance: that deletes it and its data. Don't release or disassociate the `fde-ehr-clinical` Elastic IP: the DB server needs it.

## Cost guardrails in place

- **Monthly cost budget** in AWS Budgets, with email alerts at 85%, at 100% and when forecast spend exceeds the budget. It should exclude credits so it tracks real usage; check this under the budget's Advanced options.
- **No unattached Elastic IPs.** The unused `fde-elastic-ip` was released.
- Remaining standing costs while stopped: the two EBS volumes and one Elastic IP.
