"""Confirmed ZoomInfo company relink must not compare origin notes/campaigns
against the destination company, and must refresh this client's contact links.

Run: python test_zoominfo_company_relink.py

Creates and deletes temporary NSZIRL rows. Does not change Flora Jia or Whirlpool.
Uses isolated testdb — never writes through live :8007 or northstar.db.
"""

from __future__ import annotations

import testdb
import sys
import time

from db import get_connection

FLORA_ID = 4631
WHIRLPOOL_ID = 298
MARKER = "NSZIRL"
CARMECO_ID = 1
BROWN_ID = 2


def _fail(msg: str) -> None:
    raise AssertionError(msg)


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
        SELECT id, client_id, company_id, status, assigned_user_id, next_action,
               follow_up_date, notes
        FROM client_company_relationships
        WHERE company_id = ? AND client_id = ?
        """,
        (company_id, client_id),
    ).fetchone()
    return dict(row) if row else None


def _contact_link(conn, contact_id: int, client_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT id, contact_id, client_id, relationship_id
        FROM contact_client_relationships
        WHERE contact_id = ? AND client_id = ?
        """,
        (contact_id, client_id),
    ).fetchone()
    return dict(row) if row else None


def _contact_workflow(conn, contact_id: int, client_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT id, contact_id, client_id, relationship_id, status, assigned_user_id,
               next_action, follow_up_date
        FROM contact_client_workflows
        WHERE contact_id = ? AND client_id = ?
        """,
        (contact_id, client_id),
    ).fetchone()
    return dict(row) if row else None


def _notes(conn, company_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, client_id, company_id, note_text
            FROM legacy_notes WHERE company_id = ? ORDER BY id
            """,
            (company_id,),
        ).fetchall()
    ]


def _campaign_rows(conn, company_id: int, contact_id: int) -> dict:
    return {
        "companies": [
            dict(r)
            for r in conn.execute(
                """
                SELECT campaign_id, client_id, company_id FROM campaign_companies
                WHERE company_id = ? ORDER BY campaign_id, id
                """,
                (company_id,),
            ).fetchall()
        ],
        "contacts": [
            dict(r)
            for r in conn.execute(
                """
                SELECT campaign_id, client_id, contact_id, company_id
                FROM campaign_contacts
                WHERE contact_id = ? ORDER BY campaign_id, id
                """,
                (contact_id,),
            ).fetchall()
        ],
    }


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
        campaigns = conn.execute(
            "SELECT id FROM client_campaigns WHERE campaign_name LIKE ?",
            (f"{MARKER}%",),
        ).fetchall()
        campaign_ids = [int(r["id"]) for r in campaigns]
        if campaign_ids:
            placeholders = ",".join("?" * len(campaign_ids))
            conn.execute(
                f"DELETE FROM campaign_companies WHERE campaign_id IN ({placeholders})",
                campaign_ids,
            )
            conn.execute(
                f"DELETE FROM campaign_contacts WHERE campaign_id IN ({placeholders})",
                campaign_ids,
            )
            conn.execute(
                f"DELETE FROM client_campaigns WHERE id IN ({placeholders})",
                campaign_ids,
            )
        for contact_id in set(contact_ids):
            conn.execute("DELETE FROM contact_client_relationships WHERE contact_id = ?", (contact_id,))
            conn.execute("DELETE FROM contact_client_workflows WHERE contact_id = ?", (contact_id,))
            conn.execute("DELETE FROM campaign_contacts WHERE contact_id = ?", (contact_id,))
            conn.execute("DELETE FROM activities WHERE contact_id = ?", (contact_id,))
            conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        for cid in ids:
            conn.execute("DELETE FROM campaign_companies WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM campaign_contacts WHERE company_id = ?", (cid,))
            conn.execute("DELETE FROM legacy_notes WHERE company_id = ?", (cid,))
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


def _create_company(client_id: int, name: str, website: str) -> dict:
    status, payload = testdb.http_json(
        "POST",
        "/api/companies/manual",
        {
            "client_id": client_id,
            "action": "create",
            "company_name": name,
            "website": website,
        },
    )
    if status != 200:
        _fail(f"Company create failed ({status}): {payload}")
    return payload


def main() -> None:
    stamp = str(int(time.time()))
    with get_connection() as conn:
        flora_before = _snapshot_flora(conn)
        whirl_before = _snapshot_whirlpool(conn)

    try:
        origin = _create_company(
            CARMECO_ID,
            f"{MARKER} Origin {stamp}",
            f"https://origin-{stamp}.{MARKER.lower()}.example",
        )
        origin_id = int(origin["company_id"])
        dest = _create_company(
            CARMECO_ID,
            f"{MARKER} Dest {stamp}",
            f"https://dest-{stamp}.{MARKER.lower()}.example",
        )
        dest_id = int(dest["company_id"])
        dest_name = f"{MARKER} Dest {stamp}"
        rollback_dest = _create_company(
            CARMECO_ID,
            f"{MARKER} RollbackDest {stamp}",
            f"https://rollback-dest-{stamp}.{MARKER.lower()}.example",
        )
        rollback_dest_id = int(rollback_dest["company_id"])

        linked = testdb.http_json(
            "POST",
            "/api/companies/manual",
            {
                "client_id": BROWN_ID,
                "action": "link",
                "existing_company_id": origin_id,
                "company_name": f"{MARKER} Origin {stamp}",
            },
        )
        if linked[0] != 200:
            _fail(f"Brown origin link failed ({linked[0]}): {linked[1]}")

        contact = testdb.http_json(
            "POST",
            "/api/contacts/manual",
            {
                "client_id": CARMECO_ID,
                "company_id": origin_id,
                "action": "create",
                "first_name": f"{MARKER}Pat",
                "last_name": "Relink",
                "title": "Buyer",
                "email": f"pat.relink.{stamp}@{MARKER.lower()}.test",
                "phone": f"419{(int(stamp[-7:]) + 5) % 10000000:07d}",
            },
        )
        if contact[0] != 200:
            _fail(f"Contact create failed ({contact[0]}): {contact[1]}")
        contact_id = int(contact[1]["contact_id"])

        stay = testdb.http_json(
            "POST",
            "/api/contacts/manual",
            {
                "client_id": CARMECO_ID,
                "company_id": origin_id,
                "action": "create",
                "first_name": f"{MARKER}Stay",
                "last_name": "Origin",
                "email": f"stay.origin.{stamp}@{MARKER.lower()}.test",
                "phone": f"419{(int(stamp[-7:]) + 6) % 10000000:07d}",
            },
        )
        if stay[0] != 200:
            _fail(f"Stay-behind contact create failed ({stay[0]}): {stay[1]}")
        stay_id = int(stay[1]["contact_id"])

        campaign = testdb.http_json(
            "POST",
            "/api/campaigns",
            {
                "client_id": CARMECO_ID,
                "campaign_name": f"{MARKER} Relink Campaign {stamp}",
                "status": "Active",
            },
        )
        if campaign[0] != 200:
            _fail(f"Campaign create failed ({campaign[0]}): {campaign[1]}")
        campaign_id = int(campaign[1]["campaign_id"])
        add_co = testdb.http_json(
            "POST",
            f"/api/campaigns/{campaign_id}/companies",
            {"company_id": origin_id},
        )
        if add_co[0] != 200:
            _fail(f"Campaign company add failed ({add_co[0]}): {add_co[1]}")
        add_ct = testdb.http_json(
            "POST",
            f"/api/campaigns/{campaign_id}/contacts",
            {"contact_id": contact_id},
        )
        if add_ct[0] != 200:
            _fail(f"Campaign contact add failed ({add_ct[0]}): {add_ct[1]}")

        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO legacy_notes (client_id, company_id, note_text, source_field)
                VALUES (?, ?, ?, 'Sales Rep Comments/Notes')
                """,
                (CARMECO_ID, origin_id, f"{MARKER} carmeco origin note"),
            )
            conn.execute(
                """
                INSERT INTO legacy_notes (client_id, company_id, note_text, source_field)
                VALUES (?, ?, ?, 'Sales Rep Comments/Notes')
                """,
                (BROWN_ID, origin_id, f"{MARKER} brown origin note"),
            )
            conn.commit()
            origin_ccr_carmeco = _ccr(conn, origin_id, CARMECO_ID)
            origin_ccr_brown = _ccr(conn, origin_id, BROWN_ID)
            dest_ccr_carmeco = _ccr(conn, dest_id, CARMECO_ID)
            dest_ccr_brown = _ccr(conn, dest_id, BROWN_ID)
            if origin_ccr_brown is None:
                _fail("Brown origin company relationship missing before contact assignment.")
            conn.execute(
                """
                INSERT INTO contact_client_relationships (
                    contact_id, client_id, relationship_id, created_at, created_by
                ) VALUES (?, ?, ?, datetime('now'), ?)
                """,
                (contact_id, BROWN_ID, int(origin_ccr_brown["id"]), MARKER),
            )
            conn.execute(
                """
                INSERT INTO contact_client_workflows (
                    contact_id, client_id, relationship_id, status, assigned_user_id,
                    next_action, follow_up_date, follow_up_time, updated_at
                ) VALUES (?, ?, ?, 'Working', NULL, 'Call', NULL, '', datetime('now'))
                """,
                (contact_id, BROWN_ID, int(origin_ccr_brown["id"])),
            )
            conn.commit()
            notes_before = _notes(conn, origin_id)
            campaigns_before = _campaign_rows(conn, origin_id, contact_id)
            carmeco_link_before = _contact_link(conn, contact_id, CARMECO_ID)
            brown_link_before = _contact_link(conn, contact_id, BROWN_ID)
            carmeco_wf_before = _contact_workflow(conn, contact_id, CARMECO_ID)
            brown_wf_before = _contact_workflow(conn, contact_id, BROWN_ID)
            stay_company_before = int(
                conn.execute(
                    "SELECT company_id FROM contacts WHERE id = ?", (stay_id,)
                ).fetchone()["company_id"]
            )
        if origin_ccr_carmeco is None or origin_ccr_brown is None:
            _fail("Origin company was not linked to both Carmeco and Brown.")
        if dest_ccr_carmeco is None:
            _fail("Destination company missing Carmeco relationship.")
        if dest_ccr_brown is not None:
            _fail("Destination company unexpectedly assigned to Brown.")
        if carmeco_link_before is None or carmeco_wf_before is None:
            _fail("Carmeco contact relationship/workflow missing before relink.")
        if brown_link_before is None or brown_wf_before is None:
            _fail("Brown contact relationship/workflow missing before relink.")
        if int(carmeco_link_before["relationship_id"]) != int(origin_ccr_carmeco["id"]):
            _fail("Carmeco contact link did not point at the origin CCR.")
        if int(brown_link_before["relationship_id"]) != int(origin_ccr_brown["id"]):
            _fail("Brown contact link did not point at Brown's origin CCR.")
        if not notes_before or not campaigns_before["companies"] or not campaigns_before["contacts"]:
            _fail("Expected origin notes and campaign membership before relink.")

        import zoominfo_crm_data

        orig_activity = zoominfo_crm_data.insert_activity_row

        def _boom(*_a, **_k):
            raise RuntimeError("forced relink activity failure")

        zoominfo_crm_data.insert_activity_row = _boom
        try:
            failed = testdb.http_json(
                "POST",
                f"/api/contacts/{contact_id}/zoominfo/apply",
                {
                    "client_id": CARMECO_ID,
                    "apply_fields": [],
                    "confirm_company_relink": True,
                    "target_company_id": rollback_dest_id,
                    "zoominfo": {
                        "first_name": f"{MARKER}Pat",
                        "last_name": "Relink",
                        "company_name": f"{MARKER} RollbackDest {stamp}",
                    },
                },
            )
            if failed[0] not in {400, 409, 500}:
                _fail(f"Forced relink failure expected 400/409/500, got {failed[0]}: {failed[1]}")
            with get_connection() as conn:
                still_origin = int(
                    conn.execute(
                        "SELECT company_id FROM contacts WHERE id = ?", (contact_id,)
                    ).fetchone()["company_id"]
                )
                if still_origin != origin_id:
                    _fail("Failed relink did not roll back contacts.company_id.")
                rolled_carmeco = _contact_link(conn, contact_id, CARMECO_ID)
                if rolled_carmeco != carmeco_link_before:
                    _fail("Failed relink did not roll back Carmeco contact_client_relationships.")
                rolled_wf = _contact_workflow(conn, contact_id, CARMECO_ID)
                if rolled_wf != carmeco_wf_before:
                    _fail("Failed relink did not roll back Carmeco contact_client_workflows.")
        finally:
            zoominfo_crm_data.insert_activity_row = orig_activity

        relink = testdb.http_json(
            "POST",
            f"/api/contacts/{contact_id}/zoominfo/apply",
            {
                "client_id": CARMECO_ID,
                "apply_fields": [],
                "confirm_company_relink": True,
                "target_company_id": dest_id,
                "zoominfo": {
                    "first_name": f"{MARKER}Pat",
                    "last_name": "Relink",
                    "company_name": dest_name,
                },
            },
        )
        if relink[0] != 200:
            _fail(f"Confirmed company relink failed ({relink[0]}): {relink[1]}")
        detail = str(relink[1].get("detail") or relink[1].get("message") or "")
        if "must not change notes" in detail.lower() or "must not change campaign" in detail.lower():
            _fail(f"False notes/campaigns guard fired on relink: {relink[1]}")
        if relink[1].get("company_relinked") is not True:
            _fail("Relink response did not set company_relinked.")
        if int(relink[1].get("company_id") or 0) != dest_id:
            _fail("Relink response company_id is not the destination.")

        with get_connection() as conn:
            moved = conn.execute(
                "SELECT company_id FROM contacts WHERE id = ?", (contact_id,)
            ).fetchone()
            if int(moved["company_id"]) != dest_id:
                _fail("Contact was not moved to the destination company.")
            stay_company = int(
                conn.execute(
                    "SELECT company_id FROM contacts WHERE id = ?", (stay_id,)
                ).fetchone()["company_id"]
            )
            if stay_company != stay_company_before:
                _fail("Unrelated origin contact was moved.")
            if _ccr(conn, origin_id, CARMECO_ID) != origin_ccr_carmeco:
                _fail("Relink changed Carmeco's origin company relationship.")
            if _ccr(conn, origin_id, BROWN_ID) != origin_ccr_brown:
                _fail("Relink changed Brown's origin company relationship.")
            if _ccr(conn, dest_id, BROWN_ID) is not None:
                _fail("Relink assigned Brown to the destination company.")
            dest_ccr_after = _ccr(conn, dest_id, CARMECO_ID)
            if dest_ccr_after is None:
                _fail("Carmeco destination company relationship missing after relink.")
            carmeco_link_after = _contact_link(conn, contact_id, CARMECO_ID)
            if carmeco_link_after is None:
                _fail("Carmeco contact_client_relationships missing after relink.")
            if int(carmeco_link_after["relationship_id"]) != int(dest_ccr_after["id"]):
                _fail("Carmeco contact_client_relationships still point at the origin CCR.")
            carmeco_wf_after = _contact_workflow(conn, contact_id, CARMECO_ID)
            if carmeco_wf_after is None:
                _fail("Carmeco contact_client_workflows missing after relink.")
            if int(carmeco_wf_after["relationship_id"]) != int(dest_ccr_after["id"]):
                _fail("Carmeco contact_client_workflows.relationship_id was not refreshed.")
            if carmeco_wf_after["status"] != carmeco_wf_before["status"]:
                _fail("Relink changed Carmeco contact workflow status.")
            if carmeco_wf_after["next_action"] != carmeco_wf_before["next_action"]:
                _fail("Relink changed Carmeco contact next_action.")
            if _contact_link(conn, contact_id, BROWN_ID) != brown_link_before:
                _fail("Relink changed Brown's contact_client_relationships.")
            if _contact_workflow(conn, contact_id, BROWN_ID) != brown_wf_before:
                _fail("Relink changed Brown's contact_client_workflows.")
            if _notes(conn, origin_id) != notes_before:
                _fail("Origin company notes were moved or changed.")
            if _notes(conn, dest_id):
                _fail("Notes were copied onto the destination company.")
            if _campaign_rows(conn, origin_id, contact_id) != campaigns_before:
                _fail("Origin campaign membership was moved or changed.")
            dest_campaigns = _campaign_rows(conn, dest_id, contact_id)
            if dest_campaigns["companies"]:
                _fail("Campaign company membership was copied to the destination.")
            if flora_before is not None and _snapshot_flora(conn) != flora_before:
                _fail("Flora Jia identity changed.")
            if whirl_before is not None and _snapshot_whirlpool(conn) != whirl_before:
                _fail("Whirlpool identity changed.")

        print("test_zoominfo_company_relink: ok")
    finally:
        _cleanup()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"test_zoominfo_company_relink: FAIL: {exc}", file=sys.stderr)
        raise
