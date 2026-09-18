"""CLI to provision a non-administrator staff user.

Does not invent emails or passwords. Password is read from getpass or
NORTHSTAR_STAFF_PROVISION_PASSWORD (popped immediately). Never printed.

Live northstar.db is refused unless NORTHSTAR_ALLOW_STAFF_PROVISION=1 and
--confirm-live with the exact database path.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from staff_provisioning import (
    PASSWORD_ENV,
    StaffProvisionError,
    canonicalize_email,
    provision_staff_user,
)


def _read_password(email: str) -> str:
    env_pw = os.environ.pop(PASSWORD_ENV, "")
    if env_pw:
        return env_pw
    first = getpass.getpass(f"Initial password for {email}: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise StaffProvisionError("Passwords do not match.")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Provision a NorthStar RevOps staff user (no Git secrets)."
    )
    parser.add_argument("--first-name", required=True)
    parser.add_argument("--last-name", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--role", default="revops_specialist")
    parser.add_argument("--client-id", type=int, action="append", required=True)
    parser.add_argument("--exist-ok", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args(argv)
    try:
        email = canonicalize_email(args.email)
        password = _read_password(email)
        result = provision_staff_user(
            first_name=args.first_name,
            last_name=args.last_name,
            email=email,
            password=password,
            staff_role=args.role,
            client_ids=list(args.client_id),
            exist_ok=bool(args.exist_ok),
            confirm_live=True if args.confirm_live else None,
        )
    except StaffProvisionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    user = result["user"]
    print(
        f"{'created' if result['created'] else 'existing'} "
        f"{user['full_name']} <{user['email']}> id={user['user_id']} "
        f"role={user['staff_role']} clients="
        f"{[c['code'] for c in result['client_assignments']]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
