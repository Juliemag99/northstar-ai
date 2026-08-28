"""Bounded global name matching for Add Contact (Phase 2).

Run: python test_manual_contact_name_match.py

Creates and deletes temporary NSMCN rows on the isolated testdb copy.
Does not change Flora Jia, Whirlpool, or live CRM identity values.
Never writes through live :8007 or production northstar.db.
"""

from __future__ import annotations

import testdb
import sys
import time

from db import get_connection, migrate_schema

FLORA_ID = 4631
WHIRLPOOL_ID = 298
MARKER = "NSMCN"
CARMECO_ID = 1
BROWN_ID = 2


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _snapshot_flora(conn) -> dict | None:
    row = conn.execute(
        """
        SELECT id, company_id, first_name, last_name, title, phone, alt_phone, email
        FROM contacts
        WHERE id = ? OR (first_name = 'Flora' AND last_name = 'Jia')
        ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END, id
        LIMIT 1
        """,
        (FLORA_ID, FLORA_ID),
    ).fetchone()
    return dict(row) if row else None


def _snapshot_whirlpool(conn) -> dict | None:
    row = conn.execute(
        "SELECT id, company_name, external_record_no FROM companies WHERE id = ?",
        (WHIRLPOOL_ID,),
    ).fetchone()
    return dict(row) if row else None


def _cleanup(company_ids: list[int]) -> None:
    with get_connection() as conn:
        leftover = conn.execute(
            "SELECT id FROM companies WHERE company_name LIKE ? OR external_record_no LIKE ?",
            (f"{MARKER} %", f"{MARKER}-%"),
        ).fetchall()
        ids = {int(r["id"]) for r in leftover}
        ids.update(int(cid) for cid in company_ids if cid)
        contact_ids = [
            int(r["id"])
            for cid in ids
            for r in conn.execute(
                "SELECT id FROM contacts WHERE company_id = ?", (cid,)
            ).fetchall()
        ]
        extra = conn.execute(
            "SELECT id FROM contacts WHERE first_name LIKE ? OR email LIKE ?",
            (f"{MARKER}%", f"%{MARKER.lower()}%"),
        ).fetchall()
        contact_ids.extend(int(r["id"]) for r in extra)
        for contact_id in set(contact_ids):
            conn.execute(
                "DELETE FROM contact_client_relationships WHERE contact_id = ?",
                (contact_id,),
            )
            conn.execute(
                "DELETE FROM contact_client_workflows WHERE contact_id = ?",
                (contact_id,),
            )
            conn.execute("DELETE FROM activities WHERE contact_id = ?", (contact_id,))
            conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        for cid in ids:
            conn.execute("DELETE FROM activities WHERE company_id = ?", (cid,))
            conn.execute(
                "DELETE FROM client_company_relationships WHERE company_id = ?",
                (cid,),
            )
            conn.execute("DELETE FROM contacts WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM companies WHERE id = ?", (cid,))
        conn.commit()


def _insert_company(conn, *, client_id: int, stamp: str, suffix: str) -> int:
    record_no = f"{MARKER}-{stamp}-{suffix}"
    cur = conn.execute(
        """
        INSERT INTO companies (external_record_no, company_name, city, state)
        VALUES (?, ?, 'Testville', 'OH')
        """,
        (record_no, f"{MARKER} Co {suffix} {stamp}"),
    )
    company_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO client_company_relationships (
            client_id, company_id, external_record_no, status, next_action
        ) VALUES (?, ?, ?, 'New', '')
        """,
        (client_id, company_id, record_no),
    )
    return company_id


def _insert_contact(
    conn,
    *,
    company_id: int,
    stamp: str,
    suffix: str,
    first: str,
    last: str,
    email: str = "",
    phone: str = "",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO contacts (
            company_id, external_record_no, first_name, last_name, title,
            phone, alt_phone, email, source_row_index
        ) VALUES (?, ?, ?, ?, '', ?, '', ?, 0)
        """,
        (company_id, f"{MARKER}-{stamp}-{suffix}", first, last, phone, email),
    )
    return int(cur.lastrowid)


def _preview(company_id: int, **fields: object) -> dict:
    status, payload = testdb.http_json(
        "POST",
        "/api/contacts/manual/preview",
        {"client_id": CARMECO_ID, "company_id": company_id, **fields},
    )
    if status != 200:
        _fail(f"preview failed ({status}): {payload}")
    return payload


def _match(preview: dict, contact_id: int) -> dict | None:
    for item in preview.get("matches") or []:
        if int(item.get("contact_id") or 0) == int(contact_id):
            return item
    return None


def _contact_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"])


def main() -> None:
    stamp = str(int(time.time()))
    company_ids: list[int] = []
    with get_connection() as conn:
        migrate_schema(conn)
        flora_before = _snapshot_flora(conn)
        whirl_before = _snapshot_whirlpool(conn)

    try:
        with get_connection() as conn:
            carmeco_co = _insert_company(
                conn, client_id=CARMECO_ID, stamp=stamp, suffix="carmeco"
            )
            brown_co = _insert_company(
                conn, client_id=BROWN_ID, stamp=stamp, suffix="brown"
            )
            other_carmeco_co = _insert_company(
                conn, client_id=CARMECO_ID, stamp=stamp, suffix="other"
            )
            company_ids.extend([carmeco_co, brown_co, other_carmeco_co])

            same_co_id = _insert_contact(
                conn,
                company_id=carmeco_co,
                stamp=stamp,
                suffix="sameco",
                first=f"{MARKER}Same",
                last="Company",
                email=f"same.co.{stamp}@{MARKER.lower()}.test",
            )
            blank_id = _insert_contact(
                conn,
                company_id=brown_co,
                stamp=stamp,
                suffix="blank",
                first=f"{MARKER}Cross",
                last="Blank",
            )
            email_diff_id = _insert_contact(
                conn,
                company_id=brown_co,
                stamp=stamp,
                suffix="emaildiff",
                first=f"{MARKER}Cross",
                last="Email",
                email=f"brown.email.{stamp}@{MARKER.lower()}.test",
            )
            email_exact_id = _insert_contact(
                conn,
                company_id=brown_co,
                stamp=stamp,
                suffix="emailexact",
                first=f"{MARKER}Other",
                last="Person",
                email=f"shared.email.{stamp}@{MARKER.lower()}.test",
            )
            phone_exact_id = _insert_contact(
                conn,
                company_id=brown_co,
                stamp=stamp,
                suffix="phoneexact",
                first=f"{MARKER}Phone",
                last="Only",
                phone=f"419{stamp[-7:]}",
            )
            conn.commit()

        plan = testdb.http_json("GET", "/health")
        if plan[0] != 200:
            _fail(f"testdb health failed ({plan[0]}): {plan[1]}")
        with get_connection() as conn:
            explained = conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT id FROM contacts
                WHERE last_name = ? COLLATE NOCASE
                  AND first_name = ? COLLATE NOCASE
                  AND company_id != ?
                LIMIT 40
                """,
                ("Blank", f"{MARKER}Cross", carmeco_co),
            ).fetchall()
            plan_text = " ".join(str(row["detail"]) for row in explained).lower()
            if "idx_contacts_last_first_nocase" not in plan_text:
                _fail(f"Name lookup did not use idx_contacts_last_first_nocase: {plan_text}")

        same_company = _preview(
            carmeco_co,
            first_name=f"{MARKER}Same",
            last_name="Company",
            email=f"other.same.{stamp}@{MARKER.lower()}.test",
        )
        same_hit = _match(same_company, same_co_id)
        if same_hit is None:
            _fail("Same name at the same company was not returned.")
        if same_hit.get("confidence") != "high" or not same_hit.get("same_company"):
            _fail(f"Same-company name should be high: {same_hit}")
        if same_company.get("can_create") is not False:
            _fail("Same-company name match must still block create.")

        blank_cross = _preview(
            carmeco_co,
            first_name=f"{MARKER}Cross",
            last_name="Blank",
        )
        blank_hit = _match(blank_cross, blank_id)
        if blank_hit is None:
            _fail("Same name at another company with blank email/phone was not returned.")
        if blank_hit.get("confidence") != "possible":
            _fail(f"Cross-company blank-info name should be possible: {blank_hit}")
        if blank_hit.get("same_company"):
            _fail("Cross-company name match was marked same_company.")
        if blank_cross.get("can_create") is not True:
            _fail("Cross-company name match must not block create.")

        with get_connection() as conn:
            contacts_before = _contact_count(conn)
        created = testdb.http_json(
            "POST",
            "/api/contacts/manual",
            {
                "client_id": CARMECO_ID,
                "company_id": carmeco_co,
                "action": "create",
                "first_name": f"{MARKER}Cross",
                "last_name": "Blank",
                "confirm_without_contact_info": True,
            },
        )
        if created[0] != 200:
            _fail(f"Create with cross-company name match was blocked ({created[0]}): {created[1]}")
        if int(created[1].get("contact_id") or 0) == blank_id:
            _fail("Create reused or linked the other-company contact instead of inserting.")
        with get_connection() as conn:
            if _contact_count(conn) != contacts_before + 1:
                _fail("Create did not insert a new master contact.")
            moved = conn.execute(
                "SELECT company_id FROM contacts WHERE id = ?", (blank_id,)
            ).fetchone()
            if int(moved["company_id"]) != brown_co:
                _fail("Create moved the other-company contact.")
            identity = conn.execute(
                "SELECT first_name, last_name, email, phone FROM contacts WHERE id = ?",
                (blank_id,),
            ).fetchone()
            if (
                str(identity["first_name"]).strip() != f"{MARKER}Cross"
                or str(identity["last_name"]).strip() != "Blank"
                or str(identity["email"] or "").strip() != ""
                or str(identity["phone"] or "").strip() != ""
            ):
                _fail("Create overwrote the other-company contact identity.")

        email_diff = _preview(
            other_carmeco_co,
            first_name=f"{MARKER}Cross",
            last_name="Email",
            email=f"carmeco.email.{stamp}@{MARKER.lower()}.test",
        )
        email_diff_hit = _match(email_diff, email_diff_id)
        if email_diff_hit is None:
            _fail("Same name at another company with a different email was not returned.")
        if email_diff_hit.get("confidence") != "possible":
            _fail(f"Different-email cross-company name should be possible: {email_diff_hit}")
        if email_diff.get("can_create") is not True:
            _fail("Different-email cross-company name match must not block create.")

        email_exact = _preview(
            carmeco_co,
            first_name=f"{MARKER}Unrelated",
            last_name="Name",
            email=f"shared.email.{stamp}@{MARKER.lower()}.test",
        )
        email_exact_hit = _match(email_exact, email_exact_id)
        if email_exact_hit is None:
            _fail("Exact email across companies was not returned.")
        if email_exact_hit.get("confidence") != "high":
            _fail(f"Exact email across companies should stay high: {email_exact_hit}")
        if email_exact.get("can_create") is not False:
            _fail("Exact email across companies must still block create.")

        phone_exact = _preview(
            carmeco_co,
            first_name=f"{MARKER}Unrelated",
            last_name="Phone",
            phone=f"419{stamp[-7:]}",
        )
        phone_exact_hit = _match(phone_exact, phone_exact_id)
        if phone_exact_hit is None:
            _fail("Exact phone across companies was not returned.")
        if phone_exact_hit.get("confidence") != "high":
            _fail(f"Exact phone across companies should stay high: {phone_exact_hit}")
        if phone_exact.get("can_create") is not False:
            _fail("Exact phone across companies must still block create.")

        unique = _preview(
            carmeco_co,
            first_name=f"{MARKER}Unique",
            last_name="Nobody",
            email=f"unique.{stamp}@{MARKER.lower()}.test",
        )
        if unique.get("matches"):
            _fail(f"Unique contact preview returned matches: {unique.get('matches')}")
        if unique.get("can_create") is not True:
            _fail("Unique contact with no matches should allow create.")

        with get_connection() as conn:
            flora_after = _snapshot_flora(conn)
            whirl_after = _snapshot_whirlpool(conn)
        if flora_before != flora_after:
            _fail("Flora Jia identity changed.")
        if whirl_before != whirl_after:
            _fail("Whirlpool identity changed.")

        print("test_manual_contact_name_match: ok")
    finally:
        _cleanup(company_ids)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"test_manual_contact_name_match: FAIL: {exc}", file=sys.stderr)
        raise
