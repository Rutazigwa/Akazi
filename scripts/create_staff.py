#!/usr/bin/env python
"""Create a staff account.

Needed at least once to bootstrap: without an account there is no way to log in,
and there is deliberately no self-registration endpoint on an internal admin
system.

    python scripts/create_staff.py --name "Owner" --phone +250780000001 \
        --role owner --identity-access

The password is read from the terminal without echo, or from STAFF_PASSWORD for
non-interactive provisioning. It is never taken as a command-line argument --
that would put it in the shell history and in the process list.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.auth import hash_password  # noqa: E402
from app.db import session_scope  # noqa: E402

ROLES = ("coordinator", "supervisor", "admin", "owner", "readonly")
MIN_PASSWORD_LENGTH = 12


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--phone", required=True)
    parser.add_argument("--email")
    parser.add_argument("--role", choices=ROLES, default="coordinator")
    parser.add_argument(
        "--identity-access",
        action="store_true",
        help="grant access to national ID numbers and other identity data "
             "(Law 058/2021 -- grant deliberately, not by default)",
    )
    args = parser.parse_args()

    password = os.environ.get("STAFF_PASSWORD")
    if not password:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat: "):
            print("passwords do not match", file=sys.stderr)
            return 1

    if len(password) < MIN_PASSWORD_LENGTH:
        print(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters",
            file=sys.stderr,
        )
        return 1

    with session_scope() as session:
        staff_id = session.execute(
            text(
                """
                INSERT INTO staff (full_name, phone, email, role,
                                   can_view_identity, password_hash)
                VALUES (:name, :phone, :email, CAST(:role AS staff_role),
                        :identity, :pwhash)
                RETURNING staff_id
                """
            ),
            {
                "name": args.name,
                "phone": args.phone,
                "email": args.email,
                "role": args.role,
                "identity": args.identity_access,
                "pwhash": hash_password(password),
            },
        ).scalar_one()

    print(f"created {args.role} {args.name} ({staff_id})")
    if args.identity_access:
        print("  identity access GRANTED -- every read is audited")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
