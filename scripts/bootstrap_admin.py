#!/usr/bin/env python3
"""One-time admin bootstrap: create an approved admin account.

Run from the repo root with the venv python:
    .venv/bin/python scripts/bootstrap_admin.py you@example.com
Password is prompted without echo. Safe to re-run: refuses to overwrite
an existing account.
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.auth import MIN_PASSWORD_LEN, hash_password


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: bootstrap_admin.py EMAIL")
    email = sys.argv[1].strip().lower()

    password = getpass.getpass(f"Password for {email}: ")
    if len(password) < MIN_PASSWORD_LEN:
        sys.exit(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    if getpass.getpass("Repeat password: ") != password:
        sys.exit("Passwords do not match.")

    db.migrate()
    conn = db.connect()
    try:
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            sys.exit(f"{email} already exists — not touching it.")
        conn.execute(
            """INSERT INTO users (email, password_hash, is_approved,
                                  approved_at, is_admin)
               VALUES (?, ?, 1, datetime('now'), 1)""",
            (email, hash_password(password)),
        )
        conn.commit()
    finally:
        conn.close()
    print(f"admin account created and approved: {email}")


if __name__ == "__main__":
    main()
