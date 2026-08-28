"""Add Company duplicate-check workflow.

Run: python test_add_company_duplicates.py

Uses isolated testdb. Creates/deletes NSMCD rows only. Does not change
Flora Jia, Whirlpool, or A. O. Smith identity values.
"""

from __future__ import annotations

import testdb
import sys
import time
from pathlib import Path

from db import get_connection
from manual_company_data import compact_company_name, names_match

FLORA_ID = 4631
WHIRLPOOL_ID = 298
MARKER = "NSMCD"
CARMECO_ID = 1
BROWN_ID = 2
REPO = Path(__file__).resolve().parent.parent
MODAL = REPO / "frontend" / "src" / "AddCompanyModal.tsx"


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _reject_ok(status: int, payload: dict, label: str) -> None:
    if status not in {400, 422}:
        _fail(f"{label}: expected HTTP 400 or 422, got {status}: {payload}")


def _cleanup() -> None:
    with get_connection() as conn:
        companies = conn.execute(
            "SELECT id FROM companies WHERE company_name LIKE ? OR external_record_no LIKE ?",
            (f"{MARKER} %", f"{MARKER}-%"),
        ).fetchall()
        ids = [int(r["id"]) for r in companies]
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


def _create(client_id: int, **fields: object) -> dict:
    body = {"client_id": client_id, "action": "create", **fields}
    status, payload = testdb.http_json("POST", "/api/companies/manual", body)
    if status != 200:
        _fail(f"create failed ({status}): {payload}")
    return payload


def _preview(client_id: int, **fields: object) -> dict:
    body = {"client_id": client_id, **fields}
    status, payload = testdb.http_json("POST", "/api/companies/manual/preview", body)
    if status != 200:
        _fail(f"preview failed ({status}): {payload}")
    return payload


def _match_ids(preview: dict) -> set[int]:
    return {int(m["company_id"]) for m in (preview.get("matches") or [])}


def test_normalization_helpers() -> None:
    if compact_company_name("AO Smith") != compact_company_name("A. O. Smith"):
        _fail("AO Smith did not normalize to the same value as A. O. Smith.")
    if not names_match("AO Smith", "A. O. Smith"):
        _fail("names_match failed for AO Smith / A. O. Smith.")
    if not names_match("ao smith", "A.O. SMITH"):
        _fail("Capitalization and punctuation differences did not match.")
    if not names_match("A O Smith", "AO Smith"):
        _fail("Insignificant spaces were not normalized.")


def test_modal_requires_check_before_save() -> None:
    text = MODAL.read_text(encoding="utf-8")
    if "Check for Duplicates" not in text or 'data-testid="add-company-check-duplicates"' not in text:
        _fail("Add Company modal is missing a Check for Duplicates control.")
    if "disabled={busy || !canOrdinarySave}" not in text:
        _fail("Save Company is not disabled until a current duplicate check with no matches.")
    if "setCheckedFingerprint(null)" not in text:
        _fail("Editing a match field after checking does not require another check.")
    if "Create New Anyway" not in text or "Confirm Create New Anyway" not in text:
        _fail("Create New Anyway is missing the second confirmation step.")
    if "No likely duplicates found" not in text:
        _fail("No-match copy is missing from the modal.")
    if "Open Existing Company" not in text or "Link Existing Company" not in text:
        _fail("Match actions Open/Link Existing Company are missing.")


def main() -> None:
    stamp = str(int(time.time()))
    test_normalization_helpers()
    test_modal_requires_check_before_save()

    with get_connection() as conn:
        flora_before = conn.execute(
            "SELECT id, company_id, first_name, last_name FROM contacts WHERE id = ?",
            (FLORA_ID,),
        ).fetchone()
        whirl_before = conn.execute(
            "SELECT id, company_name, external_record_no FROM companies WHERE id = ?",
            (WHIRLPOOL_ID,),
        ).fetchone()
        ao_row = conn.execute(
            """
            SELECT id, company_name, external_record_no FROM companies
            WHERE company_name = 'A. O. Smith' COLLATE NOCASE
            LIMIT 1
            """,
        ).fetchone()
        companies_before = int(conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"])
        ccr_before = int(
            conn.execute("SELECT COUNT(*) AS n FROM client_company_relationships").fetchone()["n"]
        )

    try:
        missing = testdb.http_json(
            "POST",
            "/api/companies/manual/preview",
            {"company_name": "AO Smith"},
        )
        _reject_ok(missing[0], missing[1], "preview missing client_id")
        zero = testdb.http_json(
            "POST",
            "/api/companies/manual",
            {"client_id": 0, "action": "create", "company_name": f"{MARKER} Zero {stamp}"},
        )
        _reject_ok(zero[0], zero[1], "create client_id 0")

        if ao_row is not None:
            ao_preview = _preview(CARMECO_ID, company_name="AO Smith")
            if int(ao_row["id"]) not in _match_ids(ao_preview):
                _fail("AO Smith did not match existing A. O. Smith.")
            if ao_preview.get("can_create") is not False:
                _fail("Ordinary create was still allowed when A. O. Smith matched.")

        created = _create(
            CARMECO_ID,
            company_name=f"{MARKER} A. O. Widget {stamp}",
            website=f"https://widget-{stamp}.{MARKER.lower()}.example",
            address="500 Duplicate Ave",
            city="Toledo",
            state="OH",
            zip="43604",
            phone=f"419{stamp[-7:]}",
            external_record_no=f"{MARKER}-{stamp}",
        )
        company_id = int(created["company_id"])

        name_preview = _preview(BROWN_ID, company_name=f"{MARKER} AO Widget {stamp}")
        if company_id not in _match_ids(name_preview):
            _fail("Punctuation/spacing variant did not match the created A. O. Widget company.")
        if name_preview.get("message") != "A possible match exists. Link it, or confirm Create New Anyway.":
            if not name_preview.get("matches"):
                _fail("Normalized name preview returned no matches.")
        if name_preview.get("can_create") is not False:
            _fail("Save/create was allowed before confirming a known name match.")
        match = next(m for m in name_preview["matches"] if int(m["company_id"]) == company_id)
        if not match.get("address") or "Toledo" not in f"{match.get('city')}":
            _fail("Match display is missing address/city.")
        rels = match.get("client_relationships") or []
        if not any(int(r.get("client_id") or 0) == CARMECO_ID for r in rels):
            _fail("Match did not include existing client relationships.")

        caps = _preview(BROWN_ID, company_name=f"{MARKER} a.o. widget {stamp}")
        if company_id not in _match_ids(caps):
            _fail("Capitalization/punctuation variant did not match.")

        domain_preview = _preview(
            BROWN_ID,
            company_name=f"{MARKER} Unrelated {stamp}",
            website=f"https://widget-{stamp}.{MARKER.lower()}.example",
        )
        if company_id not in _match_ids(domain_preview):
            _fail("Matching domain was not detected.")

        phone_preview = _preview(
            BROWN_ID,
            company_name=f"{MARKER} PhoneOnly {stamp}",
            phone=f"419{stamp[-7:]}",
        )
        if company_id not in _match_ids(phone_preview):
            _fail("Matching phone was not detected.")

        addr_preview = _preview(
            BROWN_ID,
            company_name=f"{MARKER} AddrOnly {stamp}",
            address="500 Duplicate Ave",
            city="Toledo",
            state="OH",
        )
        if company_id not in _match_ids(addr_preview):
            _fail("Matching address/city/state was not detected.")

        rn_preview = _preview(
            BROWN_ID,
            company_name=f"{MARKER} RecordOnly {stamp}",
            external_record_no=f"{MARKER}-{stamp}",
        )
        if company_id not in _match_ids(rn_preview):
            _fail("Matching Record No. was not detected.")

        blocked = testdb.http_json(
            "POST",
            "/api/companies/manual",
            {
                "client_id": BROWN_ID,
                "action": "create",
                "company_name": f"{MARKER} AO Widget {stamp}",
            },
        )
        _reject_ok(blocked[0], blocked[1], "create without duplicate confirmation")

        with get_connection() as conn:
            before_count = int(conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"])
            carmeco_ccr = dict(
                conn.execute(
                    """
                    SELECT id, client_id, company_id, status FROM client_company_relationships
                    WHERE company_id = ? AND client_id = ?
                    """,
                    (company_id, CARMECO_ID),
                ).fetchone()
            )

        anyway = testdb.http_json(
            "POST",
            "/api/companies/manual",
            {
                "client_id": BROWN_ID,
                "action": "create",
                "company_name": f"{MARKER} AO Widget {stamp}",
            },
        )
        _reject_ok(anyway[0], anyway[1], "Create New Anyway without confirmation")

        linked = testdb.http_json(
            "POST",
            "/api/companies/manual",
            {
                "client_id": BROWN_ID,
                "action": "link",
                "existing_company_id": company_id,
                "company_name": f"{MARKER} AO Widget {stamp}",
            },
        )
        if linked[0] != 200:
            _fail(f"link failed ({linked[0]}): {linked[1]}")
        with get_connection() as conn:
            after_link = int(conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"])
            if after_link != before_count:
                _fail("Link created a duplicate master company.")
            after_ccr = dict(
                conn.execute(
                    """
                    SELECT id, client_id, company_id, status FROM client_company_relationships
                    WHERE company_id = ? AND client_id = ?
                    """,
                    (company_id, CARMECO_ID),
                ).fetchone()
            )
            if after_ccr != carmeco_ccr:
                _fail("Link changed Carmeco's existing relationship.")
            if conn.execute(
                "SELECT 1 FROM client_company_relationships WHERE company_id = ? AND client_id = ?",
                (company_id, BROWN_ID),
            ).fetchone() is None:
                _fail("Link did not add the Brown relationship.")

        unique_name = f"{MARKER} Brand New {stamp}"
        unique_preview = _preview(CARMECO_ID, company_name=unique_name)
        if unique_preview.get("matches"):
            _fail("Unique company preview returned matches.")
        if unique_preview.get("message") != "No likely duplicates found":
            _fail("Unique preview did not report No likely duplicates found.")
        if unique_preview.get("can_create") is not True:
            _fail("Unique preview did not enable create.")

        with get_connection() as conn:
            preview_companies = int(conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"])
        if preview_companies != after_link:
            _fail("Duplicate preview created a company.")

        confirmed_dup = testdb.http_json(
            "POST",
            "/api/companies/manual",
            {
                "client_id": BROWN_ID,
                "action": "create",
                "company_name": f"{MARKER} A.O. Widget {stamp}",
                "website": f"https://widget-extra-{stamp}.{MARKER.lower()}.example",
                "phone": f"418{stamp[-7:]}",
                "confirm_create_despite_match": True,
            },
        )
        if confirmed_dup[0] != 200:
            _fail(
                f"Create New Anyway with confirmation failed ({confirmed_dup[0]}): {confirmed_dup[1]}"
            )
        if int(confirmed_dup[1].get("company_id") or 0) == company_id:
            _fail("Create New Anyway linked instead of creating after confirmation.")

        unique = _create(
            CARMECO_ID,
            company_name=unique_name,
            website=f"https://new-{stamp}.{MARKER.lower()}.example",
        )
        if int(unique["company_id"]) == company_id:
            _fail("Unique create reused the duplicate company.")

        with get_connection() as conn:
            flora_after = conn.execute(
                "SELECT id, company_id, first_name, last_name FROM contacts WHERE id = ?",
                (FLORA_ID,),
            ).fetchone()
            whirl_after = conn.execute(
                "SELECT id, company_name, external_record_no FROM companies WHERE id = ?",
                (WHIRLPOOL_ID,),
            ).fetchone()
            if flora_before is not None and dict(flora_after) != dict(flora_before):
                _fail("Flora Jia identity changed.")
            if whirl_before is not None and dict(whirl_after) != dict(whirl_before):
                _fail("Whirlpool identity changed.")
            if ao_row is not None:
                ao_after = conn.execute(
                    "SELECT id, company_name, external_record_no FROM companies WHERE id = ?",
                    (int(ao_row["id"]),),
                ).fetchone()
                if dict(ao_after) != dict(ao_row):
                    _fail("A. O. Smith identity changed.")

        print("test_add_company_duplicates: ok")
    finally:
        _cleanup()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"test_add_company_duplicates: FAIL: {exc}", file=sys.stderr)
        raise
