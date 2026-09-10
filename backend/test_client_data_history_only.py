"""Isolated tests for history-only Client Data Import.

Run from backend/:
  python test_client_data_history_only.py

Uses isolated testdb copies only — never writes production northstar.db.
"""

from __future__ import annotations

import csv
import io
import json
import os
import secrets
from pathlib import Path

import testdb
from auth_http import CSRF_HEADER
from auth_passwords import hash_password
from brown_history_event_convert import (
    convert_brown_history_csv,
    convert_note_blob,
    conversion_result_to_csv_bytes,
)
from client_data_history_match import resolve_history_company
from client_data_import import (
    IMPORT_MODE_HISTORY_ONLY,
    confirm_client_data_import,
    ensure_client_data_import_schema,
    parse_history_with_mapping,
)
from crm_import_staging import ensure_crm_import_schema
from db import DB_PATH, PRODUCTION_DB_PATH, get_connection, migrate_schema
from fastapi.testclient import TestClient
from main import app
from shared_note_history_import import ensure_shared_note_history_schema

UPLOAD = "/api/clients/{client_id}/admin/client-data-imports"
BATCH = "/api/clients/{client_id}/admin/client-data-imports/{batch_id}"
MAPPING = "/api/clients/{client_id}/admin/client-data-imports/{batch_id}/mapping"
DRY_RUN = "/api/clients/{client_id}/admin/client-data-imports/{batch_id}/dry-run"
CONFIRM = "/api/clients/{client_id}/admin/client-data-imports/{batch_id}/confirm"

FREEZE_CRM = (
    "companies",
    "contacts",
    "client_company_relationships",
    "activities",
    "legacy_notes",
)


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _prove_isolated() -> None:
    opened = Path(os.fspath(DB_PATH)).resolve()
    prod = PRODUCTION_DB_PATH.resolve()
    if opened == prod:
        _fail("Testdb isolation failed: opened production DB.")
    print(f"ISOLATED {opened}")


def _client() -> TestClient:
    return TestClient(app)


def _csrf(response) -> str:
    return str(response.json().get("csrf_token") or "")


def _create_admin() -> tuple[int, str, str]:
    email = f"hist_only_{secrets.token_hex(4)}@example.com"
    password = f"NsTest9{secrets.token_hex(8)}"
    digest = hash_password(password, email=email)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (
                email, full_name, is_administrator, is_internal_northstar, active,
                password_hash, failed_login_count, locked_until
            ) VALUES (?, ?, 1, 1, 1, ?, 0, '')
            """,
            (email, "History Only Admin", digest),
        )
        user_id = int(
            conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        )
        conn.commit()
    return user_id, email, password


def _assign(user_id: int, client_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO user_client_assignments
                (user_id, client_id, role, active, assigned_at)
            VALUES (?, ?, 'staff', 1, datetime('now'))
            """,
            (user_id, client_id),
        )
        conn.commit()


def _two_clients() -> tuple[int, int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 2").fetchall()
        if len(rows) < 2:
            conn.execute(
                "INSERT INTO clients (code, name) VALUES (?, ?)",
                (f"ho_b_{secrets.token_hex(3)}", "History Only Client B"),
            )
            conn.commit()
            rows = conn.execute("SELECT id FROM clients ORDER BY id LIMIT 2").fetchall()
    return int(rows[0]["id"]), int(rows[1]["id"])


def _freeze(tables: tuple[str, ...] = FREEZE_CRM + ("company_shared_history_events",)) -> dict[str, int]:
    with get_connection() as conn:
        return {
            t: int(conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"])
            for t in tables
        }


def _assert_frozen(before: dict[str, int], tables: tuple[str, ...] | None = None) -> None:
    check = tables or tuple(before.keys())
    after = _freeze(check)  # type: ignore[arg-type]
    for t in check:
        if after[t] != before[t]:
            _fail(f"Master table mutated during preview: {t} {before[t]} -> {after[t]}")


def _login(client: TestClient, email: str, password: str) -> str:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    if r.status_code != 200:
        _fail(f"login failed {r.status_code} {r.text}")
    return _csrf(r)


def _seed_company(
    *,
    client_id: int,
    record_no: str,
    name: str,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO companies (external_record_no, company_name, created_at, last_updated_at)
            VALUES (?, ?, datetime('now'), datetime('now'))
            """,
            (record_no, name),
        )
        company_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, external_record_no, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'New', datetime('now'), datetime('now'))
            """,
            (int(client_id), company_id, record_no),
        )
        conn.commit()
    return company_id


def _history_csv(rows: list[dict[str, str]]) -> bytes:
    headers = [
        "Record No.",
        "Contact No.",
        "Company",
        "Notes",
        "Contact_Note",
        "Updated",
        "Edited By",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({h: row.get(h, "") for h in headers})
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _hist_map() -> dict[str, str]:
    return {
        "history_record_no": "Record No.",
        "history_company_name": "Company",
        "history_contact_no": "Contact No.",
        "history_note_text": "Notes",
        "history_contact_note": "Contact_Note",
        "history_event_at": "Updated",
        "history_author": "Edited By",
    }


def test_history_only_upload_no_crm_batch_and_no_master_changes() -> None:
    client_a, _client_b = _two_clients()
    user_id, email, password = _create_admin()
    _assign(user_id, client_a)
    company_id = _seed_company(client_id=client_a, record_no="HO-1001", name="Alpha Tools")
    before = _freeze()
    crm_batches_before = _freeze(("crm_import_batches",))["crm_import_batches"]

    content = _history_csv(
        [
            {
                "Record No.": "HO-1001",
                "Contact No.": "C1",
                "Company": "Alpha Tools",
                "Notes": "Called about quote follow-up.",
                "Updated": "1-Jan-2026 9:00 AM",
                "Edited By": "Julie",
            }
        ]
    )
    with _client() as client:
        csrf = _login(client, email, password)
        up = client.post(
            UPLOAD.format(client_id=client_a),
            headers={CSRF_HEADER: csrf},
            files={"history_file": ("history.csv", content, "text/csv")},
        )
        if up.status_code != 200:
            _fail(f"upload {up.status_code} {up.text}")
        body = up.json()
        batch = body["batch"]
        if batch.get("import_mode") != IMPORT_MODE_HISTORY_ONLY:
            _fail(f"expected history_only mode, got {batch.get('import_mode')}")
        if batch.get("crm_batch_id") not in (None, 0):
            _fail(f"history-only must not create CRM batch, got {batch.get('crm_batch_id')}")
        batch_id = int(batch["batch_id"])
        mp = client.put(
            MAPPING.format(client_id=client_a, batch_id=batch_id),
            headers={CSRF_HEADER: csrf},
            json={"history_mapping": _hist_map(), "prospects_mapping": {}},
        )
        if mp.status_code != 200:
            _fail(f"map {mp.status_code} {mp.text}")
        dry = client.post(
            DRY_RUN.format(client_id=client_a, batch_id=batch_id),
            headers={CSRF_HEADER: csrf},
            json={},
        )
        if dry.status_code != 200:
            _fail(f"dry {dry.status_code} {dry.text}")
        preview = dry.json()
        if not preview.get("confirm_allowed"):
            _fail(f"confirm should be allowed: {preview.get('history')}")
        if int(preview["history"]["insert"]) != 1:
            _fail(f"expected 1 insert, got {preview['history']}")
        if int(preview["history"]["matched_by_record_no"]) != 1:
            _fail("expected record_no match")
        _assert_frozen(before)
        crm_after = _freeze(("crm_import_batches",))["crm_import_batches"]
        if crm_after != crm_batches_before:
            _fail("history-only upload created a CRM prospects batch")

        conf = client.post(
            CONFIRM.format(client_id=client_a, batch_id=batch_id),
            headers={CSRF_HEADER: csrf},
            json={"confirm": True, "plan_fingerprint": preview["plan_fingerprint"]},
        )
        if conf.status_code != 200:
            _fail(f"confirm {conf.status_code} {conf.text}")
        result = conf.json()
        if int(result.get("history_inserted") or 0) != 1:
            _fail(f"expected history insert 1, got {result}")
        if int(result.get("companies_created") or 0) != 0:
            _fail("history-only must not create companies")
        if int(result.get("contacts_created") or 0) != 0:
            _fail("history-only must not create contacts")

    # masters except shared history unchanged
    after_masters = _freeze(FREEZE_CRM)
    for t in FREEZE_CRM:
        if after_masters[t] != before[t]:
            _fail(f"history-only mutated {t}")
    with get_connection() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM company_shared_history_events WHERE company_id = ?",
            (company_id,),
        ).fetchone()["n"]
        if int(n) < 1:
            _fail("shared history not inserted")
        rn = conn.execute(
            "SELECT external_record_no FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()["external_record_no"]
        if str(rn) != "HO-1001":
            _fail("company Record No. was rewritten")
    print("PASS history_only_upload_no_crm_batch_and_no_master_changes")


def test_name_fallback_ambiguous_and_unmatched() -> None:
    client_a, client_b = _two_clients()
    # Same normalized name twice on client A → ambiguous
    _seed_company(client_id=client_a, record_no="HO-A1", name="Beta Works Inc")
    _seed_company(client_id=client_a, record_no="HO-A2", name="Beta Works")
    # Unique name on client A
    _seed_company(client_id=client_a, record_no="HO-A3", name="Gamma Steel")
    # Same display name only on client B
    _seed_company(client_id=client_b, record_no="HO-B1", name="Delta Isolated")

    with get_connection() as conn:
        unique = resolve_history_company(
            conn, client_id=client_a, record_no="", company_name="Gamma Steel Co"
        )
        if unique.status != "matched" or unique.method != "company_name":
            _fail(f"unique name fallback failed: {unique}")
        # Do not rewrite — matched company still has HO-A3
        if unique.external_record_no != "HO-A3":
            _fail("name fallback should keep existing RN")

        ambiguous = resolve_history_company(
            conn, client_id=client_a, record_no="MISSING", company_name="Beta Works Inc"
        )
        if ambiguous.status != "ambiguous":
            _fail(f"expected ambiguous, got {ambiguous}")

        unmatched = resolve_history_company(
            conn, client_id=client_a, record_no="NOPE", company_name="Delta Isolated"
        )
        if unmatched.status != "unmatched":
            _fail(f"cross-client name must not match: {unmatched}")

        by_rn = resolve_history_company(
            conn, client_id=client_a, record_no="HO-A3", company_name="Wrong Name"
        )
        if by_rn.status != "matched" or by_rn.method != "record_no":
            _fail(f"record_no should win: {by_rn}")
    print("PASS name_fallback_ambiguous_and_unmatched")


def test_blank_rows_and_contact_note_retained() -> None:
    mapping = {
        "history_record_no": "Record No.",
        "history_company_name": "Company",
        "history_note_text": "Notes",
        "history_contact_note": "Contact_Note",
    }
    csv_bytes = _history_csv(
        [
            {},  # fully blank → skipped
            {
                "Record No.": "HO-CN-1",
                "Company": "Contact Note Co",
                "Notes": "",
                "Contact_Note": "Meaningful contact note body",
            },
            {
                "Record No.": "HO-CN-2",
                "Company": "Empty History Co",
                "Notes": "",
                "Contact_Note": "",
            },
        ]
    )
    # Insert blank line by rewriting with empty row values already present.
    parsed = parse_history_with_mapping(csv_bytes, mapping, client_id=1)
    if parsed.blank_rows_skipped < 1:
        _fail(f"expected blank row skip, got {parsed.blank_rows_skipped}")
    if parsed.blank_history_skipped < 1:
        _fail(f"expected blank history skip, got {parsed.blank_history_skipped}")
    if len(parsed.events) != 1:
        _fail(f"expected 1 retained event, got {len(parsed.events)} {parsed.errors}")
    if "Meaningful contact note body" not in parsed.events[0].note_text:
        _fail("Contact_Note content was not retained")
    if parsed.errors:
        _fail(f"blank skips should not be errors: {parsed.errors}")
    print("PASS blank_rows_and_contact_note_retained")


def test_converter_split_marketing_unparseable_idempotent() -> None:
    blob = (
        "22-Apr-2026 10:30 AM(GMT -06:00) Central Time (US & Canada) - E-Marketing\n"
        "Email was sent to Sue Lamar - sue@example.com\n\n"
        "22-Apr-2026 9:20 AM CDT - Robert Kirstein (Quick Actions)\n"
        "Left Message - Fogg Ben\n\n"
        "22-Apr-2026 9:20 AM CDT - Robert Kirstein\n"
        "Karen said things are slow; call every six months.\n"
    )
    events = convert_note_blob(
        note_text=blob,
        record_no="1325547",
        contact_no="873892",
        company="Fogg Filler Co",
        original_source_row=2,
    )
    marketing = [e for e in events if e.excluded_marketing]
    kept = [e for e in events if not e.excluded_marketing]
    if not marketing:
        _fail("expected marketing exclusion from converter")
    if len(kept) < 2:
        _fail(f"expected retained non-marketing events, got {kept}")
    again = convert_note_blob(
        note_text=blob,
        record_no="1325547",
        contact_no="873892",
        company="Fogg Filler Co",
        original_source_row=2,
    )
    ids1 = [e.source_event_id for e in events if not e.excluded_marketing]
    ids2 = [e.source_event_id for e in again if not e.excluded_marketing]
    if ids1 != ids2:
        _fail("converter IDs are not deterministic")

    unparsed = convert_note_blob(
        note_text="Random freeform note without timestamp headers.",
        record_no="1",
        contact_no="2",
        company="X",
        original_source_row=9,
    )
    if len(unparsed) != 1 or unparsed[0].review_flag != "unparseable_retained":
        _fail(f"unparseable meaningful blob not retained: {unparsed}")
    print("PASS converter_split_marketing_unparseable_idempotent")


def test_idempotent_reimport_and_cross_client_isolation() -> None:
    client_a, client_b = _two_clients()
    user_id, email, password = _create_admin()
    _assign(user_id, client_a)
    _assign(user_id, client_b)
    company_a = _seed_company(client_id=client_a, record_no="HO-ISO-1", name="Iso Alpha")
    _seed_company(client_id=client_b, record_no="HO-ISO-2", name="Iso Beta")

    content = _history_csv(
        [
            {
                "Record No.": "HO-ISO-1",
                "Company": "Iso Alpha",
                "Notes": "First call note for isolation test.",
                "Updated": "2-Jan-2026 10:00 AM",
                "Edited By": "Julie",
            }
        ]
    )

    def _run(client_id: int) -> dict:
        with _client() as client:
            csrf = _login(client, email, password)
            up = client.post(
                UPLOAD.format(client_id=client_id),
                headers={CSRF_HEADER: csrf},
                files={"history_file": ("history.csv", content, "text/csv")},
            )
            batch_id = int(up.json()["batch"]["batch_id"])
            client.put(
                MAPPING.format(client_id=client_id, batch_id=batch_id),
                headers={CSRF_HEADER: csrf},
                json={"history_mapping": _hist_map(), "prospects_mapping": {}},
            )
            dry = client.post(
                DRY_RUN.format(client_id=client_id, batch_id=batch_id),
                headers={CSRF_HEADER: csrf},
                json={},
            ).json()
            conf = client.post(
                CONFIRM.format(client_id=client_id, batch_id=batch_id),
                headers={CSRF_HEADER: csrf},
                json={"confirm": True, "plan_fingerprint": dry["plan_fingerprint"]},
            )
            return {"dry": dry, "conf": conf.json(), "status": conf.status_code}

    first = _run(client_a)
    if first["status"] != 200 or int(first["conf"].get("history_inserted") or 0) != 1:
        _fail(f"first import failed: {first}")
    second = _run(client_a)
    if second["status"] != 200:
        _fail(f"reimport confirm failed: {second}")
    if int(second["dry"]["history"].get("already_present") or 0) < 1:
        _fail(f"reimport should detect duplicate: {second['dry']['history']}")
    if int(second["conf"].get("history_inserted") or 0) != 0:
        _fail("reimport must not insert duplicates")

    # Name-only fallback is client-scoped: client B must not match client A's company name.
    name_only = _history_csv(
        [
            {
                "Record No.": "",
                "Company": "Iso Alpha",
                "Notes": "Should not land on client B import.",
            }
        ]
    )
    with _client() as client:
        csrf = _login(client, email, password)
        up = client.post(
            UPLOAD.format(client_id=client_b),
            headers={CSRF_HEADER: csrf},
            files={"history_file": ("history.csv", name_only, "text/csv")},
        )
        batch_id = int(up.json()["batch"]["batch_id"])
        client.put(
            MAPPING.format(client_id=client_b, batch_id=batch_id),
            headers={CSRF_HEADER: csrf},
            json={"history_mapping": _hist_map(), "prospects_mapping": {}},
        )
        dry = client.post(
            DRY_RUN.format(client_id=client_b, batch_id=batch_id),
            headers={CSRF_HEADER: csrf},
            json={},
        ).json()
        if int(dry["history"].get("unmatched_companies") or 0) < 1:
            _fail(f"client B name fallback must not match client A company: {dry['history']}")
        if dry.get("confirm_allowed"):
            _fail("confirm must be blocked for unmatched history")

    with get_connection() as conn:
        n = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM company_shared_history_events WHERE company_id = ?",
                (company_a,),
            ).fetchone()["n"]
        )
    if n != 1:
        _fail(f"expected exactly 1 shared history on company A, got {n}")
    print("PASS idempotent_reimport_and_cross_client_isolation")


def test_multi_event_csv_roundtrip_counts() -> None:
    raw = (
        "Record No.,Contact No.,Company,Notes,Contact_Note\n"
        "1,10,Acme,\"22-Apr-2026 10:30 AM CDT - E-Marketing\n"
        "Email was sent to a@x.com\n\n"
        "22-Apr-2026 9:20 AM CDT - Pat Smith\n"
        "Called and left a message about the project.\",\n"
    ).encode("utf-8")
    result = convert_brown_history_csv(raw)
    if result.marketing_excluded < 1:
        _fail("expected marketing exclusion in conversion")
    if result.parsed_events < 1:
        _fail("expected parsed non-marketing event")
    out = conversion_result_to_csv_bytes(result)
    again = convert_brown_history_csv(raw)
    if [e.source_event_id for e in result.events] != [
        e.source_event_id for e in again.events
    ]:
        _fail("full CSV conversion not idempotent")
    if b"Source Event ID" not in out:
        _fail("output missing header")
    print("PASS multi_event_csv_roundtrip_counts")


def main() -> None:
    _prove_isolated()
    with get_connection() as conn:
        migrate_schema(conn)
        ensure_crm_import_schema(conn)
        ensure_shared_note_history_schema(conn)
        ensure_client_data_import_schema(conn)
        conn.commit()
    test_blank_rows_and_contact_note_retained()
    test_converter_split_marketing_unparseable_idempotent()
    test_name_fallback_ambiguous_and_unmatched()
    test_multi_event_csv_roundtrip_counts()
    test_history_only_upload_no_crm_batch_and_no_master_changes()
    test_idempotent_reimport_and_cross_client_isolation()
    print("ALL HISTORY-ONLY TESTS PASSED")


if __name__ == "__main__":
    main()
