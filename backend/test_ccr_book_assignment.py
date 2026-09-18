"""PR-2 CCR book-assignment tests. Isolated testdb only. Never live."""

from __future__ import annotations

import testdb  # noqa: F401

import secrets

from access import user_can_access_client
from ccr_book_assignment import (
    BookAssignmentError,
    apply_ccr_book_assignment,
    preview_ccr_book_assignment,
)
from db import get_connection
from staff_provisioning import provision_staff_user
from staff_rbac import REVOPS_SPECIALIST


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _secret() -> str:
    return f"NsTest9{secrets.token_hex(10)}"


def _client(code: str) -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM clients WHERE code = ?", (code,)).fetchone()
    if row is None:
        _fail(f"missing {code}")
    return int(row["id"])


def _ccr_count(client_id: int) -> int:
    with get_connection() as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(*) FROM client_company_relationships
                WHERE client_id = ?
                  AND TRIM(COALESCE(archived_at, '')) = ''
                """,
                (client_id,),
            ).fetchone()[0]
        )


def _owner_counts(client_id: int) -> dict[str, int]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(CAST(assigned_user_id AS TEXT), 'null') AS k, COUNT(*) AS n
            FROM client_company_relationships
            WHERE client_id = ? AND TRIM(COALESCE(archived_at, '')) = ''
            GROUP BY assigned_user_id
            """,
            (client_id,),
        ).fetchall()
    return {str(r["k"]): int(r["n"]) for r in rows}


def test_preview_does_not_write() -> None:
    brown = _client("brown")
    before = _owner_counts(brown)
    user = provision_staff_user(
        first_name="Robert",
        last_name="Kirsten",
        email=f"book.robert.{secrets.token_hex(3)}@northstar.example.test",
        password=_secret(),
        staff_role=REVOPS_SPECIALIST,
        client_ids=[brown],
    )
    preview = preview_ccr_book_assignment(
        client_id=brown, target_user_id=int(user["user"]["user_id"])
    )
    if not preview["dry_run"] or preview["applied"]:
        _fail("preview must stay dry-run")
    if preview["total_active_ccr"] != _ccr_count(brown):
        _fail("preview total drifted from live-shaped isolated CCR count")
    if preview["would_update"] <= 0:
        _fail("Brown should still be assigned to Julie in the isolated copy")
    after = _owner_counts(brown)
    if after != before:
        _fail("preview mutated assigned_user_id")


def test_apply_is_client_scoped_and_idempotent() -> None:
    brown = _client("brown")
    dawson = _client("dawson")
    premier = _client("premier")
    brown_n = _ccr_count(brown)
    dawson_before = _owner_counts(dawson)
    premier_before = _owner_counts(premier)
    robert = provision_staff_user(
        first_name="Robert",
        last_name="Kirsten",
        email=f"book.apply.{secrets.token_hex(3)}@northstar.example.test",
        password=_secret(),
        client_ids=[brown],
    )
    uid = int(robert["user"]["user_id"])
    if not user_can_access_client(uid, brown):
        _fail("ACL missing")
    applied = apply_ccr_book_assignment(client_id=brown, target_user_id=uid, dry_run=False)
    if not applied["applied"] or applied["already_assigned_to_target"] != brown_n:
        _fail(f"Brown book not fully transferred: {applied}")
    if applied["updated"] != brown_n:
        _fail(f"updated {applied['updated']} != {brown_n}")
    if _owner_counts(dawson) != dawson_before:
        _fail("Dawson CCR mutated during Brown assignment")
    if _owner_counts(premier) != premier_before:
        _fail("Premier CCR mutated during Brown assignment")
    again = apply_ccr_book_assignment(client_id=brown, target_user_id=uid, dry_run=False)
    if again["updated"] != 0 or again["already_assigned_to_target"] != brown_n:
        _fail("rerun was not idempotent")


def test_refuses_target_without_acl() -> None:
    brown = _client("brown")
    dawson = _client("dawson")
    tyler = provision_staff_user(
        first_name="Tyler",
        last_name="Sullivan",
        email=f"book.noacl.{secrets.token_hex(3)}@northstar.example.test",
        password=_secret(),
        client_ids=[dawson],
    )
    try:
        apply_ccr_book_assignment(
            client_id=brown,
            target_user_id=int(tyler["user"]["user_id"]),
            dry_run=True,
        )
    except BookAssignmentError as exc:
        if "client assignment" not in str(exc).lower():
            _fail(str(exc))
    else:
        _fail("Brown book preview allowed a Dawson-only user")


def test_refuses_unknown_client() -> None:
    dawson = _client("dawson")
    user = provision_staff_user(
        first_name="Tyler",
        last_name="Sullivan",
        email=f"book.badcid.{secrets.token_hex(3)}@northstar.example.test",
        password=_secret(),
        client_ids=[dawson],
    )
    try:
        preview_ccr_book_assignment(client_id=999999, target_user_id=int(user["user"]["user_id"]))
    except BookAssignmentError:
        pass
    else:
        _fail("unknown client accepted")


def main() -> None:
    tests = [
        test_preview_does_not_write,
        test_apply_is_client_scoped_and_idempotent,
        test_refuses_target_without_acl,
        test_refuses_unknown_client,
    ]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} tests ok")


if __name__ == "__main__":
    main()
