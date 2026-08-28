"""Manual CRM contact create/link.

Run: python test_manual_contact_create.py

Creates and deletes temporary company/contact/CCR rows (NSMCT).
Does not change Flora Jia, Whirlpool, or production identity values.
Uses the isolated testdb copy — never writes through live :8007 or northstar.db.
"""

from __future__ import annotations

import testdb
import sys
import time

from db import get_connection

FLORA_ID = 4631
WHIRLPOOL_ID = 298
MARKER = "NSMCT"
CARMECO_ID = 1
BROWN_ID = 2


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _reject_ok(status: int, payload: dict, label: str) -> None:
    if status not in {400, 422}:
        _fail(f"{label}: expected HTTP 400 or 422, got {status}: {payload}")


def _snapshot_flora(conn) -> dict | None:
    row = conn.execute(
        """
        SELECT id, company_id, first_name, last_name, title, phone, alt_phone, email,
               external_record_no, source_row_index
        FROM contacts
        WHERE id = ? OR (first_name = 'Flora' AND last_name = 'Jia')
        ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END, id
        LIMIT 1
        """,
        (FLORA_ID, FLORA_ID),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _snapshot_whirlpool(conn) -> dict | None:
    row = conn.execute(
        "SELECT id, company_name, external_record_no FROM companies WHERE id = ?",
        (WHIRLPOOL_ID,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _snapshot_ccr(conn, company_id: int, client_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT id, client_id, company_id, status, assigned_user_id, next_action, follow_up_date
        FROM client_company_relationships
        WHERE company_id = ? AND client_id = ?
        """,
        (company_id, client_id),
    ).fetchone()
    return dict(row) if row else None


def _contact_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"])


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
                "DELETE FROM contact_client_relationships WHERE relationship_id IN "
                "(SELECT id FROM client_company_relationships WHERE company_id = ?)",
                (cid,),
            )
            conn.execute(
                "DELETE FROM contact_client_workflows WHERE relationship_id IN "
                "(SELECT id FROM client_company_relationships WHERE company_id = ?)",
                (cid,),
            )
            conn.execute(
                "DELETE FROM client_company_relationships WHERE company_id = ?",
                (cid,),
            )
            conn.execute("DELETE FROM contacts WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM companies WHERE id = ?", (cid,))
        conn.commit()


def _insert_company(conn, *, client_id: int, stamp: str, suffix: str) -> tuple[int, str]:
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
    return company_id, record_no


def _activity_for(conn, contact_id: int, activity_type: str) -> dict | None:
    row = conn.execute(
        """
        SELECT activity_id, client_id, company_id, contact_id, activity_type,
               notes, created_by, created_at
        FROM activities
        WHERE contact_id = ? AND activity_type = ?
        ORDER BY activity_id DESC
        LIMIT 1
        """,
        (contact_id, activity_type),
    ).fetchone()
    return dict(row) if row else None


def _xref(conn, contact_id: int, client_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT id, contact_id, client_id, relationship_id
        FROM contact_client_relationships
        WHERE contact_id = ? AND client_id = ?
        """,
        (contact_id, client_id),
    ).fetchone()
    return dict(row) if row else None


def main() -> None:
    stamp = str(int(time.time()))
    company_ids: list[int] = []
    with get_connection() as conn:
        flora_before = _snapshot_flora(conn)
        whirl_before = _snapshot_whirlpool(conn)

    try:
        with get_connection() as conn:
            carmeco_co, _carmeco_rn = _insert_company(
                conn, client_id=CARMECO_ID, stamp=stamp, suffix="carmeco"
            )
            brown_co, _brown_rn = _insert_company(
                conn, client_id=BROWN_ID, stamp=stamp, suffix="brown"
            )
            shared_co, shared_rn = _insert_company(
                conn, client_id=CARMECO_ID, stamp=stamp, suffix="shared"
            )
            conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, external_record_no, status, next_action
                ) VALUES (?, ?, ?, 'New', '')
                """,
                (BROWN_ID, shared_co, f"{shared_rn}-brown"),
            )
            company_ids.extend([carmeco_co, brown_co, shared_co])
            brown_ccr_before = _snapshot_ccr(conn, shared_co, BROWN_ID)
            conn.commit()

        missing = testdb.http_json(
            "POST",
            "/api/contacts/manual",
            {
                "company_id": carmeco_co,
                "action": "create",
                "first_name": f"{MARKER}Ada",
                "last_name": "MissingClient",
                "email": f"ada.missing.{stamp}@{MARKER.lower()}.test",
            },
        )
        _reject_ok(missing[0], missing[1], "missing client_id")

        zero = testdb.http_json(
            "POST",
            "/api/contacts/manual",
            {
                "client_id": 0,
                "company_id": carmeco_co,
                "action": "create",
                "first_name": f"{MARKER}Ada",
                "last_name": "ZeroClient",
                "email": f"ada.zero.{stamp}@{MARKER.lower()}.test",
            },
        )
        _reject_ok(zero[0], zero[1], "client_id 0 / All My Clients")

        lookup_zero = testdb.http_json(
            "GET", f"/api/companies/lookup?client_id=0&q={MARKER}"
        )
        _reject_ok(lookup_zero[0], lookup_zero[1], "lookup All My Clients")

        no_info = testdb.http_json(
            "POST",
            "/api/contacts/manual",
            {
                "client_id": CARMECO_ID,
                "company_id": carmeco_co,
                "action": "create",
                "first_name": f"{MARKER}No",
                "last_name": "Info",
            },
        )
        _reject_ok(no_info[0], no_info[1], "create without email/phone")

        carmeco_email = f"pat.carmeco.{stamp}@{MARKER.lower()}.test"
        carmeco_phone = f"419{stamp[-7:]}"
        created = testdb.http_json(
            "POST",
            "/api/contacts/manual",
            {
                "client_id": CARMECO_ID,
                "company_id": carmeco_co,
                "action": "create",
                "first_name": f"{MARKER}Pat",
                "last_name": "Carmeco",
                "title": "Buyer",
                "email": carmeco_email,
                "phone": carmeco_phone,
            },
        )
        if created[0] != 200:
            _fail(f"Carmeco create failed ({created[0]}): {created[1]}")
        if created[1].get("action") != "created":
            _fail(f"Carmeco create action={created[1].get('action')}")
        if int(created[1].get("client_id") or 0) != CARMECO_ID:
            _fail("Carmeco create did not save under Carmeco.")
        carmeco_contact_id = int(created[1]["contact_id"])

        brown_email = f"pat.brown.{stamp}@{MARKER.lower()}.test"
        brown_phone = f"419{(int(stamp[-7:]) + 1) % 10000000:07d}"
        brown_created = testdb.http_json(
            "POST",
            "/api/contacts/manual",
            {
                "client_id": BROWN_ID,
                "company_id": brown_co,
                "action": "create",
                "first_name": f"{MARKER}Pat",
                "last_name": "Brown",
                "email": brown_email,
                "phone": brown_phone,
            },
        )
        if brown_created[0] != 200:
            _fail(f"Brown create failed ({brown_created[0]}): {brown_created[1]}")
        if int(brown_created[1].get("client_id") or 0) != BROWN_ID:
            _fail("Brown create did not save under Brown.")
        brown_contact_id = int(brown_created[1]["contact_id"])

        with get_connection() as conn:
            act = _activity_for(conn, carmeco_contact_id, "Contact Created")
            if act is None:
                _fail("Carmeco create did not record Contact Created activity.")
            if int(act["client_id"]) != CARMECO_ID:
                _fail("Contact Created activity was not Carmeco-scoped.")
            if not act.get("created_by") or not act.get("created_at"):
                _fail("Contact Created activity missing user or timestamp.")
            if _xref(conn, carmeco_contact_id, CARMECO_ID) is None:
                _fail("Carmeco create did not insert client relationship.")
            if _xref(conn, carmeco_contact_id, BROWN_ID) is not None:
                _fail("Carmeco create leaked a Brown contact relationship.")
            if _activity_for(conn, brown_contact_id, "Contact Created") is None:
                _fail("Brown create did not record Contact Created activity.")

        preview_email = testdb.http_json(
            "POST",
            "/api/contacts/manual/preview",
            {
                "client_id": CARMECO_ID,
                "company_id": carmeco_co,
                "first_name": f"{MARKER}Other",
                "last_name": "Name",
                "email": carmeco_email,
            },
        )
        if preview_email[0] != 200:
            _fail(f"Email preview failed ({preview_email[0]}): {preview_email[1]}")
        email_matches = preview_email[1].get("matches") or []
        if not any(int(m.get("contact_id") or 0) == carmeco_contact_id for m in email_matches):
            _fail("Duplicate email was not detected.")
        if preview_email[1].get("can_create") is not False:
            _fail("Duplicate email preview still allowed create.")

        preview_phone = testdb.http_json(
            "POST",
            "/api/contacts/manual/preview",
            {
                "client_id": CARMECO_ID,
                "company_id": carmeco_co,
                "first_name": f"{MARKER}Phone",
                "last_name": "Match",
                "phone": f"({carmeco_phone[:3]}) {carmeco_phone[3:6]}-{carmeco_phone[6:]}",
            },
        )
        if preview_phone[0] != 200:
            _fail(f"Phone preview failed ({preview_phone[0]}): {preview_phone[1]}")
        phone_matches = preview_phone[1].get("matches") or []
        if not any(int(m.get("contact_id") or 0) == carmeco_contact_id for m in phone_matches):
            _fail("Duplicate phone was not detected.")

        dup_create = testdb.http_json(
            "POST",
            "/api/contacts/manual",
            {
                "client_id": CARMECO_ID,
                "company_id": carmeco_co,
                "action": "create",
                "first_name": f"{MARKER}Dup",
                "last_name": "Email",
                "email": carmeco_email,
            },
        )
        _reject_ok(dup_create[0], dup_create[1], "create duplicate email")

        with get_connection() as conn:
            contacts_before_link = _contact_count(conn)
            brown_shared_before = _snapshot_ccr(conn, shared_co, BROWN_ID)
            carmeco_shared_before = _snapshot_ccr(conn, shared_co, CARMECO_ID)

        shared_created = testdb.http_json(
            "POST",
            "/api/contacts/manual",
            {
                "client_id": CARMECO_ID,
                "company_id": shared_co,
                "action": "create",
                "first_name": f"{MARKER}Sam",
                "last_name": "Shared",
                "email": f"sam.shared.{stamp}@{MARKER.lower()}.test",
                "phone": "9375550103",
            },
        )
        if shared_created[0] != 200:
            _fail(f"Shared Carmeco create failed ({shared_created[0]}): {shared_created[1]}")
        shared_contact_id = int(shared_created[1]["contact_id"])

        with get_connection() as conn:
            if _snapshot_ccr(conn, shared_co, BROWN_ID) != brown_shared_before:
                _fail("Carmeco create changed Brown's relationship on the shared company.")

        linked = testdb.http_json(
            "POST",
            "/api/contacts/manual",
            {
                "client_id": BROWN_ID,
                "company_id": shared_co,
                "action": "link",
                "existing_contact_id": shared_contact_id,
                "first_name": f"{MARKER}Sam",
                "last_name": "Shared",
                "email": f"sam.shared.{stamp}@{MARKER.lower()}.test",
            },
        )
        if linked[0] != 200:
            _fail(f"Brown link failed ({linked[0]}): {linked[1]}")
        if linked[1].get("action") != "linked":
            _fail(f"Brown link action={linked[1].get('action')}")
        if int(linked[1].get("contact_id") or 0) != shared_contact_id:
            _fail("Link created or returned a different contact id.")

        with get_connection() as conn:
            if _contact_count(conn) != contacts_before_link + 1:
                _fail("Link duplicated the master contact.")
            if _xref(conn, shared_contact_id, BROWN_ID) is None:
                _fail("Link did not add Brown's contact relationship.")
            if _xref(conn, shared_contact_id, CARMECO_ID) is None:
                _fail("Link removed Carmeco's contact relationship.")
            if _snapshot_ccr(conn, shared_co, CARMECO_ID) != carmeco_shared_before:
                _fail("Brown link changed Carmeco's company relationship.")
            if _snapshot_ccr(conn, shared_co, BROWN_ID) != brown_ccr_before:
                _fail("Brown link changed Brown's existing company relationship fields.")
            link_act = _activity_for(conn, shared_contact_id, "Contact Linked")
            if link_act is None:
                _fail("Link did not record Contact Linked activity.")
            if int(link_act["client_id"]) != BROWN_ID:
                _fail("Contact Linked activity was not Brown-scoped.")
            master = conn.execute(
                "SELECT first_name, last_name, email FROM contacts WHERE id = ?",
                (shared_contact_id,),
            ).fetchone()
            if _blank_row_name(master) != f"{MARKER}Sam Shared":
                _fail("Link altered the shared master contact name.")

        lookup = testdb.http_json(
            "GET",
            f"/api/companies/lookup?client_id={CARMECO_ID}&q={MARKER}-{stamp}-carmeco",
        )
        if lookup[0] != 200:
            _fail(f"Carmeco company lookup failed ({lookup[0]}): {lookup[1]}")
        hits = lookup[1].get("companies") or []
        if not any(int(h.get("id") or 0) == carmeco_co for h in hits):
            _fail("Carmeco lookup did not return the test company.")
        if any(int(h.get("id") or 0) == brown_co for h in hits):
            _fail("Carmeco lookup returned a Brown-only company.")

        import manual_contact_data

        orig_insert = manual_contact_data.insert_activity_row

        def _boom(*_args, **_kwargs):
            raise RuntimeError("forced activity failure")

        manual_contact_data.insert_activity_row = _boom
        try:
            with get_connection() as conn:
                contacts_before_fail = _contact_count(conn)
            failed = testdb.http_json(
                "POST",
                "/api/contacts/manual",
                {
                    "client_id": CARMECO_ID,
                    "company_id": carmeco_co,
                    "action": "create",
                    "first_name": f"{MARKER}Rollback",
                    "last_name": "Case",
                    "email": f"rollback.{stamp}@{MARKER.lower()}.test",
                    "phone": f"419{(int(stamp[-7:]) + 9) % 10000000:07d}",
                },
            )
            if failed[0] not in {400, 409, 500}:
                _fail(f"Forced failure expected HTTP 400/409/500, got {failed[0]}: {failed[1]}")
            with get_connection() as conn:
                if _contact_count(conn) != contacts_before_fail:
                    _fail("Failed create did not roll back the contact insert.")
                leftover = conn.execute(
                    "SELECT id FROM contacts WHERE email = ?",
                    (f"rollback.{stamp}@{MARKER.lower()}.test",),
                ).fetchone()
                if leftover is not None:
                    _fail("Failed create left a contact row behind.")
        finally:
            manual_contact_data.insert_activity_row = orig_insert

        confirmed = testdb.http_json(
            "POST",
            "/api/contacts/manual",
            {
                "client_id": CARMECO_ID,
                "company_id": carmeco_co,
                "action": "create",
                "first_name": f"{MARKER}Quiet",
                "last_name": "Person",
                "confirm_without_contact_info": True,
            },
        )
        if confirmed[0] != 200:
            _fail(f"Confirmed no-info create failed ({confirmed[0]}): {confirmed[1]}")

        with get_connection() as conn:
            flora_after = _snapshot_flora(conn)
            whirl_after = _snapshot_whirlpool(conn)
        if flora_before != flora_after:
            _fail("Flora Jia identity changed.")
        if whirl_before != whirl_after:
            _fail("Whirlpool identity changed.")

        print("test_manual_contact_create: ok")
    finally:
        _cleanup(company_ids)


def _blank_row_name(row) -> str:
    if row is None:
        return ""
    return f"{str(row['first_name']).strip()} {str(row['last_name']).strip()}".strip()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"test_manual_contact_create: FAIL: {exc}", file=sys.stderr)
        raise
