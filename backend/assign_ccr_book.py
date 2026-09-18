"""CLI for client-scoped CCR book-assignment preview/apply.

Default is dry-run. Live apply requires NORTHSTAR_ALLOW_CCR_BOOK_ASSIGN=1
and --apply --confirm-live.
"""

from __future__ import annotations

import argparse
import json
import sys

from ccr_book_assignment import (
    ALLOW_FLAG,
    BookAssignmentError,
    apply_ccr_book_assignment,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preview or apply a client-scoped CCR book assignment."
    )
    parser.add_argument("--client-id", type=int, required=True)
    parser.add_argument("--target-user-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true", help="Write (still refused on live without --confirm-live).")
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = apply_ccr_book_assignment(
            client_id=args.client_id,
            target_user_id=args.target_user_id,
            dry_run=not args.apply,
            confirm_live=True if args.confirm_live else None,
        )
    except BookAssignmentError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    if args.apply and result.get("database_kind", "").startswith("LIVE"):
        if not args.confirm_live:
            print(f"Live apply still requires --confirm-live and {ALLOW_FLAG}=1.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
