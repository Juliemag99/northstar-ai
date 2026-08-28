"""Manual company create/link and ZoomInfo contact update/add.

Run: python test_manual_company_zoominfo.py

Creates and deletes temporary NSMCZ rows. Does not change Flora Jia or Whirlpool.
Uses isolated testdb — never writes through live :8007 or northstar.db.
"""

from __future__ import annotations

import testdb
import sys
import time

from db import get_connection

FLORA_ID = 4631
WHIRLPOOL_ID = 298
MARKER = "NSMCZ"
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
        SELECT id, company_id, first_name, last_name, title, phone, alt_phone, email
        FROM contacts WHERE id = ? LIMIT 1
        """,
        (FLORA_ID,),
    ).fetchone()
    return dict(row) if row else None


def _snapshot_whirlpool(conn) -> dict | None:
    row = conn.execute(
        "SELECT id, company_name, external_record_no FROM companies WHERE id = ?",
        (WHIRLPOOL_ID,),
    ).fetchone()
    return dict(row) if row else None


def _ccr(conn, company_id: int, client_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT id, client_id, company_id, status, next_action, follow_up_date
        FROM client_company_relationships
        WHERE company_id = ? AND client_id = ?
        """,
        (company_id, client_id),
    ).fetchone()
    return dict(row) if row else None


def _cleanup() -> None:
    with get_connection() as conn:
        companies = conn.execute(
            "SELECT id FROM companies WHERE company_name LIKE ? OR external_record_no LIKE ?",
            (f"{MARKER} %", f"{MARKER}-%"),
        ).fetchall()
        ids = [int(r["id"]) for r in companies]
        extra = conn.execute(
            "SELECT id FROM contacts WHERE first_name LIKE ? OR email LIKE ?",
            (f"{MARKER}%", f"%{MARKER.lower()}%"),
        ).fetchall()
        contact_ids = [int(r["id"]) for r in extra]
        for cid in ids:
            contact_ids.extend(
                int(r["id"])
                for r in conn.execute(
                    "SELECT id FROM contacts WHERE company_id = ?", (cid,)
                ).fetchall()
            )
        for contact_id in set(contact_ids):
            conn.execute("DELETE FROM contact_client_relationships WHERE contact_id = ?", (contact_id,))
            conn.execute("DELETE FROM contact_client_workflows WHERE contact_id = ?", (contact_id,))
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
            conn.execute("DELETE FROM client_company_relationships WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM contacts WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM companies WHERE id = ?", (cid,))
        conn.commit()


def main() -> None:
    stamp = str(int(time.time()))
    with get_connection() as conn:
        flora_before = _snapshot_flora(conn)
        whirl_before = _snapshot_whirlpool(conn)

    try:
        missing = testdb.http_json(
            "POST",
            "/api/companies/manual",
            {"action": "create", "company_name": f"{MARKER} Missing"},
        )
        _reject_ok(missing[0], missing[1], "company missing client_id")

        zero = testdb.http_json(
            "POST",
            "/api/companies/manual",
            {"client_id": 0, "action": "create", "company_name": f"{MARKER} Zero"},
        )
        _reject_ok(zero[0], zero[1], "company client_id 0")

        unknown = testdb.http_json(
            "POST",
            "/api/companies/manual",
            {"client_id": 999999, "action": "create", "company_name": f"{MARKER} Unknown"},
        )
        _reject_ok(unknown[0], unknown[1], "unknown client_id")

        created = testdb.http_json(
            "POST",
            "/api/companies/manual",
            {
                "client_id": CARMECO_ID,
                "action": "create",
                "company_name": f"{MARKER} Acme {stamp}",
                "website": f"https://acme-{stamp}.{MARKER.lower()}.example",
                "address": "100 Test Ave",
                "city": "Toledo",
                "state": "OH",
                "zip": "43604",
                "phone": f"419{stamp[-7:]}",
                "industry": "Manufacturing",
                "employee_size": "50-99",
                "sales_volume": "$10M",
                "notes": f"{MARKER} carmeco note",
            },
        )
        if created[0] != 200:
            _fail(f"Carmeco company create failed ({created[0]}): {created[1]}")
        if created[1].get("action") != "created":
            _fail(f"Carmeco company action={created[1].get('action')}")
        if int(created[1].get("client_id") or 0) != CARMECO_ID:
            _fail("Company create did not save under Carmeco.")
        company_id = int(created[1]["company_id"])
        record_no = created[1]["external_record_no"]

        brown_co = testdb.http_json(
            "POST",
            "/api/companies/manual",
            {
                "client_id": BROWN_ID,
                "action": "create",
                "company_name": f"{MARKER} BrownCo {stamp}",
                "website": f"https://brown-{stamp}.{MARKER.lower()}.example",
                "phone": f"419{(int(stamp[-7:]) + 2) % 10000000:07d}",
            },
        )
        if brown_co[0] != 200:
            _fail(f"Brown company create failed ({brown_co[0]}): {brown_co[1]}")
        if int(brown_co[1].get("client_id") or 0) != BROWN_ID:
            _fail("Brown company create did not save under Brown.")

        with get_connection() as conn:
            act = conn.execute(
                """
                SELECT activity_type, client_id, notes, created_by, created_at
                FROM activities WHERE company_id = ? AND activity_type = 'Company Created'
                """,
                (company_id,),
            ).fetchone()
            if act is None:
                _fail("Company Created activity missing.")
            if int(act["client_id"]) != CARMECO_ID:
                _fail("Company Created activity was not Carmeco-scoped.")
            if _ccr(conn, company_id, BROWN_ID) is not None:
                _fail("Carmeco company create leaked a Brown CCR.")
            brown_ccr_before = _ccr(conn, int(brown_co[1]["company_id"]), BROWN_ID)

        preview = testdb.http_json(
            "POST",
            "/api/companies/manual/preview",
            {
                "client_id": BROWN_ID,
                "company_name": f"{MARKER} Acme {stamp}",
                "website": f"https://acme-{stamp}.{MARKER.lower()}.example",
            },
        )
        if preview[0] != 200:
            _fail(f"Company preview failed ({preview[0]}): {preview[1]}")
        matches = preview[1].get("matches") or []
        if not any(int(m.get("company_id") or 0) == company_id for m in matches):
            _fail("Company duplicate detection missed the existing company.")
        if preview[1].get("can_create") is not False:
            _fail("High-confidence company match still allowed create.")

        dup = testdb.http_json(
            "POST",
            "/api/companies/manual",
            {
                "client_id": BROWN_ID,
                "action": "create",
                "company_name": f"{MARKER} Acme {stamp}",
                "website": f"https://acme-{stamp}.{MARKER.lower()}.example",
            },
        )
        _reject_ok(dup[0], dup[1], "create duplicate company")

        with get_connection() as conn:
            companies_before_link = int(
                conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"]
            )
            carmeco_ccr_before = _ccr(conn, company_id, CARMECO_ID)

        linked = testdb.http_json(
            "POST",
            "/api/companies/manual",
            {
                "client_id": BROWN_ID,
                "action": "link",
                "existing_company_id": company_id,
                "company_name": f"{MARKER} Acme {stamp}",
            },
        )
        if linked[0] != 200:
            _fail(f"Brown company link failed ({linked[0]}): {linked[1]}")
        if linked[1].get("action") != "linked":
            _fail(f"Link action={linked[1].get('action')}")
        if int(linked[1].get("company_id") or 0) != company_id:
            _fail("Link created a different company.")

        with get_connection() as conn:
            after_count = int(conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"])
            if after_count != companies_before_link:
                _fail("Company link duplicated the master company.")
            if _ccr(conn, company_id, BROWN_ID) is None:
                _fail("Link did not add Brown's company relationship.")
            if _ccr(conn, company_id, CARMECO_ID) != carmeco_ccr_before:
                _fail("Brown company link changed Carmeco's relationship.")
            link_act = conn.execute(
                """
                SELECT client_id FROM activities
                WHERE company_id = ? AND activity_type = 'Company Linked'
                """,
                (company_id,),
            ).fetchone()
            if link_act is None or int(link_act["client_id"]) != BROWN_ID:
                _fail("Company Linked activity missing or not Brown-scoped.")
            if _ccr(conn, int(brown_co[1]["company_id"]), BROWN_ID) != brown_ccr_before:
                _fail("Link changed an unrelated Brown company relationship.")

        contact = testdb.http_json(
            "POST",
            "/api/contacts/manual",
            {
                "client_id": CARMECO_ID,
                "company_id": company_id,
                "action": "create",
                "first_name": f"{MARKER}Pat",
                "last_name": "Zoom",
                "title": "Buyer",
                "email": f"pat.zoom.{stamp}@{MARKER.lower()}.test",
                "phone": f"419{(int(stamp[-7:]) + 3) % 10000000:07d}",
            },
        )
        if contact[0] != 200:
            _fail(f"Manual contact create failed ({contact[0]}): {contact[1]}")
        contact_id = int(contact[1]["contact_id"])

        zi = {
            "first_name": f"{MARKER}Patricia",
            "last_name": "Zoom",
            "title": "Purchasing Manager",
            "email": f"patricia.zoom.{stamp}@{MARKER.lower()}.test",
            "phone": "",
            "alt_phone": "4195550199",
            "company_name": "Other ZoomInfo Co",
            "linkedin_url": "https://linkedin.com/in/example-nsmcz",
            "location": "Toledo, OH",
            "zoominfo_contact_id": f"ZI-{stamp}-ct",
        }
        prev = testdb.http_json(
            "POST",
            f"/api/contacts/{contact_id}/zoominfo/preview",
            {"client_id": CARMECO_ID, "zoominfo": zi},
        )
        if prev[0] != 200:
            _fail(f"ZoomInfo preview failed ({prev[0]}): {prev[1]}")
        if not prev[1].get("available"):
            _fail("ZoomInfo preview with payload was marked unavailable.")
        if not prev[1].get("different_company"):
            _fail("Different-company ZoomInfo warning was not raised.")
        title_field = next(
            (f for f in prev[1].get("fields") or [] if f.get("field") == "title"), None
        )
        if title_field is None or title_field.get("keep_northstar") is not True:
            _fail("ZoomInfo fields did not default to keeping NorthStar values.")
        phone_field = next(
            (f for f in prev[1].get("fields") or [] if f.get("field") == "phone"), None
        )
        if phone_field is None or phone_field.get("blank_zoominfo") is not True:
            _fail("Blank ZoomInfo phone was not flagged.")

        with get_connection() as conn:
            before_row = dict(
                conn.execute(
                    "SELECT title, email, phone, alt_phone, first_name FROM contacts WHERE id = ?",
                    (contact_id,),
                ).fetchone()
            )
            before_wf = [
                dict(r)
                for r in conn.execute(
                    "SELECT status, next_action FROM contact_client_workflows WHERE contact_id = ?",
                    (contact_id,),
                ).fetchall()
            ]

        blank_apply = testdb.http_json(
            "POST",
            f"/api/contacts/{contact_id}/zoominfo/apply",
            {
                "client_id": CARMECO_ID,
                "apply_fields": ["phone", "title"],
                "zoominfo": zi,
            },
        )
        if blank_apply[0] != 200:
            _fail(f"ZoomInfo apply failed ({blank_apply[0]}): {blank_apply[1]}")
        if "phone" in (blank_apply[1].get("changed_fields") or []):
            _fail("Blank ZoomInfo phone erased NorthStar phone.")
        if "title" not in (blank_apply[1].get("changed_fields") or []):
            _fail("Selected ZoomInfo title was not applied.")

        unselected = testdb.http_json(
            "POST",
            f"/api/contacts/{contact_id}/zoominfo/apply",
            {
                "client_id": CARMECO_ID,
                "apply_fields": [],
                "zoominfo": zi,
            },
        )
        if unselected[0] != 200:
            _fail(f"Empty apply_fields failed ({unselected[0]}): {unselected[1]}")
        if unselected[1].get("changed_fields"):
            _fail("Unselected ZoomInfo fields were applied.")

        move = testdb.http_json(
            "POST",
            f"/api/contacts/{contact_id}/zoominfo/apply",
            {
                "client_id": CARMECO_ID,
                "apply_fields": ["company"],
                "zoominfo": zi,
            },
        )
        _reject_ok(move[0], move[1], "automatic company move")

        with get_connection() as conn:
            after_row = dict(
                conn.execute(
                    "SELECT title, email, phone, first_name, source, source_updated_at, zoominfo_contact_id FROM contacts WHERE id = ?",
                    (contact_id,),
                ).fetchone()
            )
            if after_row["phone"] != before_row["phone"]:
                _fail("NorthStar phone changed by blank ZoomInfo value.")
            if after_row["email"] != before_row["email"]:
                _fail("Unselected ZoomInfo email overwrote NorthStar.")
            if after_row["first_name"] != before_row["first_name"]:
                _fail("Unselected first name was changed.")
            if after_row["title"] != "Purchasing Manager":
                _fail("Selected title was not updated.")
            if after_row.get("source") != "ZoomInfo":
                _fail("ZoomInfo source metadata was not stored.")
            if not after_row.get("source_updated_at"):
                _fail("source_updated_at was not stored.")
            if after_row.get("zoominfo_contact_id") != f"ZI-{stamp}-ct":
                _fail("zoominfo_contact_id was not stored.")
            after_wf = [
                dict(r)
                for r in conn.execute(
                    "SELECT status, next_action FROM contact_client_workflows WHERE contact_id = ?",
                    (contact_id,),
                ).fetchall()
            ]
            if after_wf != before_wf:
                _fail("ZoomInfo update changed contact workflow.")
            zi_act = conn.execute(
                """
                SELECT notes, client_id FROM activities
                WHERE contact_id = ? AND activity_type = 'Contact Updated from ZoomInfo'
                ORDER BY activity_id DESC LIMIT 1
                """,
                (contact_id,),
            ).fetchone()
            if zi_act is None:
                _fail("Contact Updated from ZoomInfo activity missing.")
            if int(zi_act["client_id"]) != CARMECO_ID:
                _fail("ZoomInfo activity was not Carmeco-scoped.")
            if "title" not in str(zi_act["notes"]):
                _fail("ZoomInfo activity did not list changed fields.")

        zi_dup_email = testdb.http_json(
            "POST",
            f"/api/contacts/{contact_id}/zoominfo/apply",
            {
                "client_id": CARMECO_ID,
                "apply_fields": ["email"],
                "zoominfo": {
                    **zi,
                    "email": f"pat.zoom.{stamp}@{MARKER.lower()}.test",
                },
            },
        )
        if zi_dup_email[0] != 200:
            # same email on same contact is fine
            pass

        other = testdb.http_json(
            "POST",
            "/api/contacts/manual",
            {
                "client_id": CARMECO_ID,
                "company_id": company_id,
                "action": "create",
                "first_name": f"{MARKER}Other",
                "last_name": "Person",
                "email": f"other.{stamp}@{MARKER.lower()}.test",
                "phone": f"419{(int(stamp[-7:]) + 4) % 10000000:07d}",
            },
        )
        if other[0] != 200:
            _fail(f"Second contact create failed ({other[0]}): {other[1]}")
        dup_email_apply = testdb.http_json(
            "POST",
            f"/api/contacts/{contact_id}/zoominfo/apply",
            {
                "client_id": CARMECO_ID,
                "apply_fields": ["email"],
                "zoominfo": {**zi, "email": f"other.{stamp}@{MARKER.lower()}.test"},
            },
        )
        _reject_ok(dup_email_apply[0], dup_email_apply[1], "ZoomInfo duplicate email")

        no_zi = testdb.http_json(
            "POST",
            f"/api/contacts/{contact_id}/zoominfo/preview",
            {"client_id": 0},
        )
        _reject_ok(no_zi[0], no_zi[1], "ZoomInfo preview All My Clients")

        add_prev = testdb.http_json(
            "POST",
            "/api/zoominfo/add/preview",
            {
                "client_id": BROWN_ID,
                "kind": "company",
                "zoominfo": {
                    "company_name": f"{MARKER} ZI New {stamp}",
                    "website": f"https://zinew-{stamp}.{MARKER.lower()}.example",
                    "zoominfo_company_id": f"ZI-{stamp}-co",
                },
            },
        )
        if add_prev[0] != 200:
            _fail(f"ZoomInfo add preview failed ({add_prev[0]}): {add_prev[1]}")
        if add_prev[1].get("can_create") is not True:
            _fail("New ZoomInfo company preview blocked create.")

        added = testdb.http_json(
            "POST",
            "/api/zoominfo/add",
            {
                "client_id": BROWN_ID,
                "action": "create",
                "kind": "company",
                "zoominfo": {
                    "company_name": f"{MARKER} ZI New {stamp}",
                    "website": f"https://zinew-{stamp}.{MARKER.lower()}.example",
                    "zoominfo_company_id": f"ZI-{stamp}-co",
                },
            },
        )
        if added[0] != 200:
            _fail(f"ZoomInfo add company failed ({added[0]}): {added[1]}")
        zi_company_id = int(added[1]["company_id"])
        with get_connection() as conn:
            zi_row = conn.execute(
                "SELECT zoominfo_company_id, source FROM companies WHERE id = ?",
                (zi_company_id,),
            ).fetchone()
            if zi_row is None or zi_row["zoominfo_company_id"] != f"ZI-{stamp}-co":
                _fail("ZoomInfo company id was not stored.")
            if zi_row["source"] != "ZoomInfo":
                _fail("ZoomInfo company source was not stored.")

        import manual_company_data

        orig = manual_company_data.insert_activity_row

        def _boom(*_a, **_k):
            raise RuntimeError("forced activity failure")

        manual_company_data.insert_activity_row = _boom
        try:
            with get_connection() as conn:
                before_n = int(conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"])
            failed = testdb.http_json(
                "POST",
                "/api/companies/manual",
                {
                    "client_id": CARMECO_ID,
                    "action": "create",
                    "company_name": f"{MARKER} Rollback {stamp}",
                    "website": f"https://rollback-{stamp}.{MARKER.lower()}.example",
                },
            )
            if failed[0] not in {400, 409, 500}:
                _fail(f"Forced company failure expected 400/409/500, got {failed[0]}: {failed[1]}")
            with get_connection() as conn:
                after_n = int(conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"])
                if after_n != before_n:
                    _fail("Failed company create did not roll back.")
        finally:
            manual_company_data.insert_activity_row = orig

        with get_connection() as conn:
            if _snapshot_flora(conn) != flora_before:
                _fail("Flora Jia identity changed.")
            if _snapshot_whirlpool(conn) != whirl_before:
                _fail("Whirlpool identity changed.")

        print("test_manual_company_zoominfo: ok")
    finally:
        _cleanup()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"test_manual_company_zoominfo: FAIL: {exc}", file=sys.stderr)
        raise
