#!/usr/bin/env python3
"""Approve (or list) pending accounts — the beta admin-approval gate.

    .venv/bin/python scripts/approve_user.py                 # list pending
    .venv/bin/python scripts/approve_user.py user@email ADMIN_EMAIL
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db


def main() -> None:
    conn = db.connect()
    try:
        if len(sys.argv) == 1:
            rows = conn.execute(
                "SELECT email, created_at FROM users WHERE is_approved = 0").fetchall()
            if not rows:
                print("no pending accounts")
            for r in rows:
                print(f"  {r['email']}  (registered {r['created_at']})")
            return

        if len(sys.argv) != 3:
            sys.exit(__doc__)
        email, admin_email = (a.strip().lower() for a in sys.argv[1:3])
        admin = conn.execute(
            "SELECT id FROM users WHERE email = ? AND is_admin = 1",
            (admin_email,)).fetchone()
        if admin is None:
            sys.exit(f"no admin account with email {admin_email}")
        updated = conn.execute(
            """UPDATE users SET is_approved = 1, approved_at = datetime('now'),
                                approved_by = ?
               WHERE email = ? AND is_approved = 0""",
            (admin["id"], email)).rowcount
        conn.commit()
        print(f"approved: {email}" if updated else
              f"{email} not found or already approved")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
