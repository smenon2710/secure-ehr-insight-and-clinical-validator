"""Create/update app users and their patient assignments.

Examples:
  # Clinician assigned to the first 3 embedded patients (password prompted)
  python scripts/07_manage_users.py add dr_lee --role clinician --first-n 3
  # Clinician assigned to specific patients
  python scripts/07_manage_users.py add dr_patel --role clinician --patients <subject_id>,<subject_id>
  # Compliance auditor (no patient access)
  python scripts/07_manage_users.py add compliance --role auditor
  python scripts/07_manage_users.py list
  python scripts/07_manage_users.py remove dr_lee
"""
import argparse
import getpass
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.api.audit import apply_security_schema
from src.api.auth import hash_password
from src.api.db import get_db_engine


def add_user(args):
    password = getpass.getpass(f"Password for {args.username}: ")
    if len(password) < 10:
        sys.exit("Password must be at least 10 characters.")
    if password != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match.")

    with get_db_engine().begin() as conn:
        conn.execute(text("""
            INSERT INTO app_users (username, password_hash, role) VALUES (:u, :h, :r)
            ON CONFLICT (username) DO UPDATE SET password_hash = EXCLUDED.password_hash, role = EXCLUDED.role
        """), {"u": args.username, "h": hash_password(password), "r": args.role})

        conn.execute(text("DELETE FROM patient_access WHERE username = :u"), {"u": args.username})
        if args.role == "clinician":
            if args.patients:
                patient_ids = [int(p) for p in args.patients.split(",")]
            else:
                patient_ids = [r[0] for r in conn.execute(text("""
                    SELECT DISTINCT subject_id FROM patient_encounters
                    WHERE clinical_embedding IS NOT NULL ORDER BY subject_id LIMIT :n
                """), {"n": args.first_n})]
            for pid in patient_ids:
                conn.execute(text("INSERT INTO patient_access (username, subject_id) VALUES (:u, :p)"),
                             {"u": args.username, "p": pid})
            print(f"✅ {args.username} (clinician) assigned to {len(patient_ids)} patients: {patient_ids}")
        else:
            print(f"✅ {args.username} (auditor) created; auditors have no patient access")


def list_users(_):
    with get_db_engine().connect() as conn:
        for row in conn.execute(text("""
            SELECT u.username, u.role, COUNT(pa.subject_id) AS patients
            FROM app_users u LEFT JOIN patient_access pa USING (username)
            GROUP BY u.username, u.role ORDER BY u.username
        """)):
            print(f"{row.username:<20}{row.role:<11}{row.patients} patients")


def remove_user(args):
    with get_db_engine().begin() as conn:
        deleted = conn.execute(text("DELETE FROM app_users WHERE username = :u"), {"u": args.username}).rowcount
    print(f"Removed {args.username}" if deleted else f"No user named {args.username}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="create or update a user")
    add.add_argument("username")
    add.add_argument("--role", choices=["clinician", "auditor"], required=True)
    add.add_argument("--patients", help="comma-separated subject_ids (clinicians)")
    add.add_argument("--first-n", type=int, default=3, help="assign the first N embedded patients (clinicians)")
    add.set_defaults(func=add_user)

    sub.add_parser("list", help="list users").set_defaults(func=list_users)

    remove = sub.add_parser("remove", help="delete a user")
    remove.add_argument("username")
    remove.set_defaults(func=remove_user)

    args = parser.parse_args()
    apply_security_schema()
    args.func(args)


if __name__ == "__main__":
    main()
