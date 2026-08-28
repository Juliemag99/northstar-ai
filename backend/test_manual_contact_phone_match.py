"""Indexed canonical phone matching for Add Contact (Phase 3a/3b).

Run: python test_manual_contact_phone_match.py

Creates and deletes temporary NSMCP rows on the isolated testdb copy.
Does not change Flora Jia, Whirlpool, or live CRM identity values.
Never writes through live :8007 or production northstar.db.
"""

from __future__ import annotations

import testdb
import sys
import time

from contact_phone import (
    canonical_contact_phone,
    format_us_phone_display,
    lookup_contact_phone_matches,
)
from db import get_connection, migrate_schema

FLORA_ID = 4631
WHIRLPOOL_ID = 298
MARKER = "NSMCP"
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
                "DELETE FROM contact_phone_keys WHERE contact_id = ?",
                (contact_id,),
            )
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
    alt_phone: str = "",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO contacts (
            company_id, external_record_no, first_name, last_name, title,
            phone, alt_phone, email, source_row_index
        ) VALUES (?, ?, ?, ?, '', ?, ?, ?, 0)
        """,
        (company_id, f"{MARKER}-{stamp}-{suffix}", first, last, phone, alt_phone, email),
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


_PHONE_SCHEMA_NAMES = (
    "contact_phone_keys",
    "idx_contact_phone_keys_nanp10",
    "idx_contact_phone_keys_last7",
    "trg_contact_phone_keys_ai",
    "trg_contact_phone_keys_au",
)


def _schema_row(conn, name: str) -> dict | None:
    row = conn.execute(
        """
        SELECT type, name, tbl_name, rootpage, sql
        FROM sqlite_master
        WHERE name = ?
        """,
        (name,),
    ).fetchone()
    return dict(row) if row else None


def _index_roots(conn) -> dict[str, int]:
    return {
        "idx_contact_phone_keys_nanp10": int(
            _schema_row(conn, "idx_contact_phone_keys_nanp10")["rootpage"]
        ),
        "idx_contact_phone_keys_last7": int(
            _schema_row(conn, "idx_contact_phone_keys_last7")["rootpage"]
        ),
    }


def _require_phone_schema(conn) -> None:
    for name in _PHONE_SCHEMA_NAMES:
        if _schema_row(conn, name) is None:
            _fail(f"Missing phone schema object after migrate: {name}")


def _reset_phone_schema(conn) -> None:
    """Return the isolated copy to a live-like state: no derived phone keys."""
    conn.execute("DROP TRIGGER IF EXISTS trg_contact_phone_keys_au")
    conn.execute("DROP TRIGGER IF EXISTS trg_contact_phone_keys_ai")
    conn.execute("DROP INDEX IF EXISTS idx_contact_phone_keys_nanp10")
    conn.execute("DROP INDEX IF EXISTS idx_contact_phone_keys_last7")
    conn.execute("DROP TABLE IF EXISTS contact_phone_keys")
    conn.commit()


def _assert_canonicalizer() -> list[str]:
    examples: list[str] = []
    cases = [
        ("(419) 555-0100", "4195550100", "(419) 555-0100"),
        ("419-555-0100", "4195550100", "(419) 555-0100"),
        ("4195550100", "4195550100", "(419) 555-0100"),
        ("+1 419 555 0100", "4195550100", "(419) 555-0100"),
        ("1-419-555-0100", "4195550100", "(419) 555-0100"),
        ("419-555-0100 x12", "4195550100", "(419) 555-0100 x12"),
        ("(419) 555-0100 ext. 12", "4195550100", "(419) 555-0100 x12"),
        ("", "", ""),
        ("   ", "", ""),
    ]
    for raw, expect_nanp, expect_display in cases:
        nanp10, last7 = canonical_contact_phone(raw)
        display = format_us_phone_display(raw)
        if nanp10 != expect_nanp:
            _fail(f"canonical {raw!r} -> nanp10 {nanp10!r}, expected {expect_nanp!r}")
        if expect_nanp and last7 != expect_nanp[-7:]:
            _fail(f"canonical {raw!r} -> last7 {last7!r}")
        if display != expect_display:
            _fail(f"display {raw!r} -> {display!r}, expected {expect_display!r}")
        examples.append(f"{raw!r} -> {display!r} / nanp10={nanp10 or '(blank)'}")

    a_nanp, a_last = canonical_contact_phone("(419) 555-0100")
    b_nanp, b_last = canonical_contact_phone("(614) 555-0100")
    if a_nanp == b_nanp:
        _fail("Different area codes must not share nanp10.")
    if a_last != b_last:
        _fail("Different area codes with the same local number should share last7.")
    seven_nanp, seven_last = canonical_contact_phone("5550100")
    if seven_nanp != "" or seven_last != "5550100":
        _fail(f"Seven-digit local should be last7-only, got {(seven_nanp, seven_last)}")
    return examples


def main() -> None:
    examples = _assert_canonicalizer()
    print("display-format examples:")
    for line in examples:
        print(f"  {line}")

    stamp = str(int(time.time()))
    last4 = stamp[-4:]
    nanp = f"419555{last4}"
    other_area = f"614555{last4}"
    formatted = f"(419) 555-{last4}"
    dashed = f"419-555-{last4}"
    plus_one = f"+1 419 555-{last4}"
    with_ext = f"{dashed} x12"
    local7 = f"555{last4}"
    company_ids: list[int] = []

    with get_connection() as conn:
        samples_before = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, phone, alt_phone FROM contacts
                WHERE trim(phone) != ''
                ORDER BY id
                LIMIT 8
                """
            ).fetchall()
        ]
        # testdb already migrated on import; reset so this measures first apply.
        _reset_phone_schema(conn)
        if _schema_row(conn, "contact_phone_keys") is not None:
            _fail("Reset left contact_phone_keys in place.")
        started = time.time()
        migrate_schema(conn)
        first_ms = int((time.time() - started) * 1000)
        _require_phone_schema(conn)
        keys_after = int(
            conn.execute("SELECT COUNT(*) AS n FROM contact_phone_keys").fetchone()["n"]
        )
        contacts_with_phone = int(
            conn.execute(
                """
                SELECT COUNT(*) AS n FROM contacts
                WHERE trim(phone) != '' OR trim(alt_phone) != ''
                """
            ).fetchone()["n"]
        )
        for sample in samples_before:
            after = conn.execute(
                "SELECT phone, alt_phone FROM contacts WHERE id = ?",
                (sample["id"],),
            ).fetchone()
            if after is None:
                _fail(f"Backfill removed contact {sample['id']}.")
            if str(after["phone"]) != str(sample["phone"]) or str(
                after["alt_phone"]
            ) != str(sample["alt_phone"]):
                _fail(
                    f"Backfill rewrote display phones for contact {sample['id']}: "
                    f"{dict(sample)} -> {dict(after)}"
                )
        roots_after_first = _index_roots(conn)
        started = time.time()
        migrate_schema(conn)
        second_ms = int((time.time() - started) * 1000)
        roots_after_second = _index_roots(conn)
        if roots_after_second != roots_after_first:
            _fail(
                "Second migrate rebuilt phone indexes: "
                f"{roots_after_first} -> {roots_after_second}"
            )
        keys_after_second = int(
            conn.execute("SELECT COUNT(*) AS n FROM contact_phone_keys").fetchone()["n"]
        )
        if keys_after_second != keys_after:
            _fail(
                f"Second migrate changed key count: {keys_after} -> {keys_after_second}"
            )
        flora_before = _snapshot_flora(conn)
        whirl_before = _snapshot_whirlpool(conn)

        nanp_plan = " ".join(
            str(row["detail"])
            for row in conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT DISTINCT k.contact_id
                FROM contact_phone_keys k
                WHERE k.nanp10 = ?
                LIMIT 20
                """,
                (nanp,),
            ).fetchall()
        )
        last7_plan = " ".join(
            str(row["detail"])
            for row in conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT DISTINCT k.contact_id
                FROM contact_phone_keys k
                WHERE k.last7 = ? AND k.nanp10 != ?
                LIMIT 20
                """,
                (nanp[-7:], nanp),
            ).fetchall()
        )
        contact_total = int(
            conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]
        )
        print(
            "isolated first migrate: "
            f"contacts={contact_total} contacts_with_phone={contacts_with_phone} "
            f"keys={keys_after} elapsed_ms={first_ms}"
        )
        print(
            "isolated second migrate: "
            f"elapsed_ms={second_ms} index_roots_preserved={roots_after_second}"
        )
        print(f"EXPLAIN nanp10: {nanp_plan}")
        print(f"EXPLAIN last7: {last7_plan}")
        if "idx_contact_phone_keys_nanp10" not in nanp_plan.lower():
            _fail(f"nanp10 lookup did not use idx_contact_phone_keys_nanp10: {nanp_plan}")
        if "idx_contact_phone_keys_last7" not in last7_plan.lower():
            _fail(f"last7 lookup did not use idx_contact_phone_keys_last7: {last7_plan}")
        if "scan contacts" in nanp_plan.lower() or "scan contacts" in last7_plan.lower():
            _fail("Phone lookup scanned contacts instead of contact_phone_keys.")

        company_id = int(
            conn.execute("SELECT id FROM companies ORDER BY id LIMIT 1").fetchone()["id"]
        )
        trig_id = _insert_contact(
            conn,
            company_id=company_id,
            stamp=stamp,
            suffix="trig",
            first=f"{MARKER}Trig",
            last="Insert",
            phone="419-555-0199",
        )
        key = conn.execute(
            """
            SELECT nanp10, last7 FROM contact_phone_keys
            WHERE contact_id = ? AND slot = 'phone'
            """,
            (trig_id,),
        ).fetchone()
        if key is None or str(key["nanp10"]) != "4195550199" or str(key["last7"]) != "5550199":
            _fail(f"INSERT trigger did not write keys: {None if key is None else dict(key)}")
        stored = conn.execute("SELECT phone FROM contacts WHERE id = ?", (trig_id,)).fetchone()
        if str(stored["phone"]) != "419-555-0199":
            _fail("INSERT trigger rewrote display phone.")
        conn.execute(
            "UPDATE contacts SET phone = ?, alt_phone = ? WHERE id = ?",
            ("+1 614 555-0199 x12", "(216) 555-0100", trig_id),
        )
        by_slot = {
            str(r["slot"]): dict(r)
            for r in conn.execute(
                """
                SELECT slot, nanp10, last7 FROM contact_phone_keys
                WHERE contact_id = ?
                """,
                (trig_id,),
            ).fetchall()
        }
        if by_slot.get("phone", {}).get("nanp10") != "6145550199":
            _fail(f"UPDATE trigger phone key wrong: {by_slot}")
        if by_slot.get("alt_phone", {}).get("nanp10") != "2165550100":
            _fail(f"UPDATE trigger alt_phone key wrong: {by_slot}")
        after_update = conn.execute(
            "SELECT phone, alt_phone FROM contacts WHERE id = ?", (trig_id,)
        ).fetchone()
        if str(after_update["phone"]) != "+1 614 555-0199 x12":
            _fail("UPDATE trigger rewrote display phone.")
        high_ids = {
            cid
            for cid, _reasons, conf in lookup_contact_phone_matches(conn, "614-555-0199")
            if conf == "high"
        }
        if trig_id not in high_ids:
            _fail("Exact 10-digit match missing after trigger update.")
        possible = [
            (cid, reasons, conf)
            for cid, reasons, conf in lookup_contact_phone_matches(
                conn, "(440) 555-0199"
            )
            if cid == trig_id
        ]
        if (
            not possible
            or possible[0][2] != "possible"
            or "phone_last7" not in possible[0][1]
        ):
            _fail(f"Last-seven-only should be possible after trigger update: {possible}")
        conn.execute(
            "UPDATE contacts SET phone = '', alt_phone = '' WHERE id = ?",
            (trig_id,),
        )
        leftover = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM contact_phone_keys WHERE contact_id = ?",
                (trig_id,),
            ).fetchone()["n"]
        )
        if leftover != 0:
            _fail("Blanking phones did not remove keys via UPDATE trigger.")
        conn.execute(
            "UPDATE contacts SET phone = ? WHERE id = ?",
            ("(419) 555-0199", trig_id),
        )
        conn.execute("DELETE FROM contacts WHERE id = ?", (trig_id,))
        cascaded = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM contact_phone_keys WHERE contact_id = ?",
                (trig_id,),
            ).fetchone()["n"]
        )
        if cascaded != 0:
            _fail("DELETE did not cascade contact_phone_keys.")
        conn.commit()
        if _index_roots(conn) != roots_after_first:
            _fail("Trigger tests rebuilt phone indexes.")

    try:
        with get_connection() as conn:
            carmeco_co = _insert_company(
                conn, client_id=CARMECO_ID, stamp=stamp, suffix="carmeco"
            )
            brown_co = _insert_company(
                conn, client_id=BROWN_ID, stamp=stamp, suffix="brown"
            )
            company_ids.extend([carmeco_co, brown_co])
            formatted_id = _insert_contact(
                conn,
                company_id=brown_co,
                stamp=stamp,
                suffix="fmt",
                first=f"{MARKER}Fmt",
                last="Phone",
                phone=formatted,
            )
            dashed_twin_id = _insert_contact(
                conn,
                company_id=brown_co,
                stamp=stamp,
                suffix="shared2",
                first=f"{MARKER}Share",
                last="Two",
                phone=dashed,
            )
            other_area_id = _insert_contact(
                conn,
                company_id=brown_co,
                stamp=stamp,
                suffix="area",
                first=f"{MARKER}Area",
                last="Code",
                phone=f"(614) 555-{last4}",
            )
            blank_id = _insert_contact(
                conn,
                company_id=brown_co,
                stamp=stamp,
                suffix="blank",
                first=f"{MARKER}Blank",
                last="Phone",
            )
            conn.commit()

        fmt_preview = _preview(
            carmeco_co,
            first_name=f"{MARKER}Unrelated",
            last_name="Format",
            phone=dashed,
        )
        fmt_hit = _match(fmt_preview, formatted_id)
        if fmt_hit is None:
            _fail("Formatted (xxx) xxx-xxxx stored phone was not found from dashed input.")
        if fmt_hit.get("confidence") != "high" or "phone_exact" not in (
            fmt_hit.get("reasons") or []
        ):
            _fail(f"Formatted equivalent should be high phone_exact: {fmt_hit}")
        if fmt_hit.get("phone") != formatted:
            _fail(f"Match display phone should be {formatted!r}, got {fmt_hit.get('phone')!r}")
        if fmt_preview.get("can_create") is not False:
            _fail("Exact 10-digit phone must block create.")

        plus_preview = _preview(
            carmeco_co,
            first_name=f"{MARKER}Unrelated",
            last_name="Plus",
            phone=plus_one,
        )
        if _match(plus_preview, formatted_id) is None:
            _fail("Country-code +1 equivalent was not matched.")
        if plus_preview.get("can_create") is not False:
            _fail("Country-code equivalent must stay high-confidence.")

        ext_preview = _preview(
            carmeco_co,
            first_name=f"{MARKER}Unrelated",
            last_name="Ext",
            phone=with_ext,
        )
        if _match(ext_preview, formatted_id) is None:
            _fail("Extension-bearing equivalent was not matched.")
        if ext_preview.get("can_create") is not False:
            _fail("Extension should be ignored for exact 10-digit matching.")

        blank_preview = _preview(
            carmeco_co,
            first_name=f"{MARKER}Blank",
            last_name="Phone",
        )
        if _match(blank_preview, blank_id) is None:
            _fail("Blank-phone same-name cross-company match was not returned.")
        blank_phone_hit = _match(blank_preview, formatted_id)
        if blank_phone_hit is not None and "phone_exact" in (
            blank_phone_hit.get("reasons") or []
        ):
            _fail("Blank phone must not produce a phone match.")
        if blank_preview.get("can_create") is not True:
            _fail("Blank phones should not block create via phone matching.")

        shared = _preview(
            carmeco_co,
            first_name=f"{MARKER}Unrelated",
            last_name="Shared",
            phone=nanp,
        )
        if _match(shared, formatted_id) is None or _match(shared, dashed_twin_id) is None:
            _fail("Shared business number did not return both contacts.")
        for cid in (formatted_id, dashed_twin_id):
            hit = _match(shared, cid)
            if hit is None or hit.get("confidence") != "high":
                _fail(f"Shared 10-digit should be high for contact {cid}: {hit}")
        if shared.get("can_create") is not False:
            _fail("Shared exact 10-digit number must block create.")

        area_preview = _preview(
            carmeco_co,
            first_name=f"{MARKER}Unrelated",
            last_name="Area",
            phone=formatted,
        )
        area_hit = _match(area_preview, other_area_id)
        if area_hit is None:
            _fail("Different area code with the same last seven was not returned.")
        if area_hit.get("confidence") != "possible" or "phone_last7" not in (
            area_hit.get("reasons") or []
        ):
            _fail(f"Different area codes should be possible last7: {area_hit}")
        exact_still_high = _match(area_preview, formatted_id)
        if exact_still_high is None or exact_still_high.get("confidence") != "high":
            _fail("Exact 10-digit match must remain high alongside last7 possibles.")
        if area_preview.get("can_create") is not False:
            _fail("Exact 10-digit still present must continue to block create.")

        last7_only = _preview(
            carmeco_co,
            first_name=f"{MARKER}Unrelated",
            last_name="Local",
            phone=f"(216) 555-{last4}",
        )
        last7_hit = _match(last7_only, formatted_id)
        if last7_hit is None:
            _fail("Last-seven-only different area code was not returned.")
        if last7_hit.get("confidence") != "possible":
            _fail(f"Last-seven-only should be possible: {last7_hit}")
        if last7_only.get("can_create") is not True:
            _fail("Last-seven-only matches must not block create.")

        seven_preview = _preview(
            carmeco_co,
            first_name=f"{MARKER}Unrelated",
            last_name="Seven",
            phone=local7,
        )
        seven_hit = _match(seven_preview, formatted_id)
        if seven_hit is None or seven_hit.get("confidence") != "possible":
            _fail(f"Seven-digit vs 10-digit should be possible: {seven_hit}")
        if seven_preview.get("can_create") is not True:
            _fail("Seven-digit vs 10-digit must not block create.")

        with get_connection() as conn:
            contacts_before = _contact_count(conn)
            other_phone_before = conn.execute(
                "SELECT phone, alt_phone FROM contacts WHERE id = ?",
                (other_area_id,),
            ).fetchone()
        created = testdb.http_json(
            "POST",
            "/api/contacts/manual",
            {
                "client_id": CARMECO_ID,
                "company_id": carmeco_co,
                "action": "create",
                "first_name": f"{MARKER}New",
                "last_name": "Last7",
                "phone": f"216-555-{last4}",
            },
        )
        if created[0] != 200:
            _fail(f"Create with last7 possible match was blocked ({created[0]}): {created[1]}")
        new_id = int(created[1].get("contact_id") or 0)
        if new_id in {formatted_id, dashed_twin_id, other_area_id, blank_id}:
            _fail("Create reused an existing contact instead of inserting.")
        with get_connection() as conn:
            if _contact_count(conn) != contacts_before + 1:
                _fail("Create did not insert a new master contact.")
            stored = conn.execute(
                "SELECT phone, alt_phone FROM contacts WHERE id = ?",
                (new_id,),
            ).fetchone()
            if str(stored["phone"]) != f"(216) 555-{last4}":
                _fail(
                    f"Create did not store (xxx) xxx-xxxx display format: {stored['phone']!r}"
                )
            other_after = conn.execute(
                "SELECT phone, alt_phone FROM contacts WHERE id = ?",
                (other_area_id,),
            ).fetchone()
            if dict(other_after) != dict(other_phone_before):
                _fail("Create overwrote the other-area contact phone.")
            key_row = conn.execute(
                "SELECT nanp10, last7 FROM contact_phone_keys WHERE contact_id = ? AND slot = 'phone'",
                (new_id,),
            ).fetchone()
            if key_row is None or str(key_row["nanp10"]) != f"216555{last4}":
                _fail(f"Create did not sync contact_phone_keys: {key_row}")

        linked = testdb.http_json(
            "POST",
            "/api/contacts/manual",
            {
                "client_id": CARMECO_ID,
                "company_id": carmeco_co,
                "action": "link",
                "existing_contact_id": formatted_id,
                "first_name": f"{MARKER}Fmt",
                "last_name": "Phone",
            },
        )
        if linked[0] != 200:
            _fail(f"Link failed ({linked[0]}): {linked[1]}")
        with get_connection() as conn:
            linked_row = conn.execute(
                "SELECT phone, alt_phone FROM contacts WHERE id = ?",
                (formatted_id,),
            ).fetchone()
            if str(linked_row["phone"]) != formatted:
                _fail("Link changed the existing contact phone.")

        with get_connection() as conn:
            flora_after = _snapshot_flora(conn)
            whirl_after = _snapshot_whirlpool(conn)
            if _index_roots(conn) != roots_after_first:
                _fail("Later migrate/ensure_schema rebuilt phone indexes.")
        if flora_before != flora_after:
            _fail("Flora Jia identity changed.")
        if whirl_before != whirl_after:
            _fail("Whirlpool identity changed.")

        print("test_manual_contact_phone_match: ok")
    finally:
        _cleanup(company_ids)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"test_manual_contact_phone_match: FAIL: {exc}", file=sys.stderr)
        raise
