#!/usr/bin/env python3
"""
One-time migration: hash any existing plaintext dashboard_passwords in the clinics table.

Clinic owners' actual passwords DO NOT change — only how they are stored in the DB.
After this script runs, the backwards-compat check in _verify_password() is no longer needed
for any migrated clinic (login will use check_password_hash going forward).

Usage:
    python migrate_passwords.py
"""

import os, sys
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

sys.path.insert(0, os.path.dirname(__file__))
from db import get_db
from werkzeug.security import generate_password_hash


def main():
    print("\nVicere — Dashboard Password Migration")
    print("=" * 42)

    migrated = 0
    skipped  = 0
    empty    = 0

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, dashboard_password FROM clinics ORDER BY id")
        rows = cur.fetchall()

        if not rows:
            print("  No clinics found.")
            return

        for clinic_id, pwd in rows:
            if not pwd:
                print(f"  SKIP  {clinic_id} — no password set")
                empty += 1
                continue
            if pwd.startswith(("pbkdf2:", "scrypt:")):
                print(f"  OK    {clinic_id} — already hashed")
                skipped += 1
                continue
            hashed = generate_password_hash(pwd)
            cur.execute(
                "UPDATE clinics SET dashboard_password = %s WHERE id = %s",
                (hashed, clinic_id),
            )
            print(f"  HASH  {clinic_id}")
            migrated += 1

    print("=" * 42)
    print(f"  Hashed:  {migrated}")
    print(f"  Already hashed: {skipped}")
    print(f"  No password: {empty}")
    print("\nDone. Clinic login credentials are unchanged.\n")


if __name__ == "__main__":
    main()
