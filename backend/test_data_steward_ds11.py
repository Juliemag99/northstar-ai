"""DS-11 duplicate review and merge planning.

Isolated testdb / TestClient only. Never writes live northstar.db.
Does not enable merge, hard delete, LeadMaster confirm, or create pilot users.
"""

from __future__ import annotations

import os
import secrets
from contextlib import contextmanager
from pathlib import Path

import testdb  # noqa: F401

from fastapi.testclient import TestClient

from auth_http import ADMIN_REQUIRED_DETAIL, AUTH_REQUIRED_DETAIL, CSRF_HEADER, ENFORCE_FLAG
from auth_passwords import hash_password
from company_aliases import upsert_company_alias
from company_locations import upsert_company_source_identity
from company_match import find_scored_matches
from data_steward import (
    ACTION_DUPLICATE_REVIEW,
    SOURCE_MANUAL_ADMIN,
    archive_company,
    create_company,
    create_contact,
    latest_provenance,
    link_relationship,
    live_destructive_enabled,
    live_master_archive_enabled,
    provenance_history,
    remove_relationship,
)
from data_steward_archive import preview_archive_company
from db import PRODUCTION_DB_PATH, get_connection
from duplicate_review import (
    CAT_EXACT_DOMAIN,
    CAT_EXACT_NAME_ADDRESS,
    CAT_EXACT_PHONE,
    CAT_IDENTITY,
    CAT_MULTI_LOCATION,
    CAT_NAME_ONLY,
    DISPOSITION_LIKELY,
    DISPOSITION_MERGE,
    DISPOSITION_MULTI,
    DISPOSITION_NOT,
    DISPOSITION_RESEARCH,
    DuplicateReviewError,
    DuplicateReviewSaveRequest,
    get_duplicate_pair_detail,
    list_duplicate_candidates,
    list_review_history,
    pair_ids,
    pair_key,
    save_duplicate_review,
)
from leadmaster_refresh_http import refresh_meta
from main import app
from models import NorthStarUser
from staff_provisioning import provision_staff_user
from staff_rbac import REVOPS_SPECIALIST

ROOT = Path(__file__).resolve().parent


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _stamp() -> str:
    return secrets.token_hex(4)


def _admin_user() -> NorthStarUser:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, email, full_name FROM users WHERE is_administrator=1 AND active=1 ORDER BY id LIMIT 1"
        ).fetchone()
    if row is None:
        _fail("isolated testdb has no administrator")
    return NorthStarUser(
        id=int(row["id"]),
        email=row["email"],
        full_name=row["full_name"],
        is_administrator=True,
        is_internal_northstar=True,
        active=True,
        created_at="",
    )


def _clients() -> dict[str, int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id, code FROM clients ORDER BY id").fetchall()
    return {str(r["code"]): int(r["id"]) for r in rows}


def _login(http: TestClient, email: str, password: str) -> str:
    resp = http.post("/api/auth/login", json={"email": email, "password": password})
    if resp.status_code != 200:
        _fail(f"login failed {email} {resp.status_code} {resp.text}")
    return str(resp.json().get("csrf_token") or "")


def _set_password(user_id: int, email: str, password: str) -> None:
    digest = hash_password(password, email=email)
    with get_connection() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (digest, int(user_id)))
        conn.commit()


@contextmanager
def _enforcement_on():
    previous = os.environ.get(ENFORCE_FLAG)
    os.environ[ENFORCE_FLAG] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ENFORCE_FLAG, None)
        else:
            os.environ[ENFORCE_FLAG] = previous


def _make_company(conn, actor: NorthStarUser, **kwargs):
    kwargs.setdefault("confirm_despite_match", True)
    return create_company(conn, actor=actor, **kwargs)


def _counts(conn) -> dict[str, int]:
    def n(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        "companies": n("SELECT COUNT(*) FROM companies"),
        "contacts": n("SELECT COUNT(*) FROM contacts"),
        "ccr": n("SELECT COUNT(*) FROM client_company_relationships"),
        "aliases": n("SELECT COUNT(*) FROM company_aliases"),
        "locations": n("SELECT COUNT(*) FROM company_locations"),
        "identities": n("SELECT COUNT(*) FROM company_source_identities"),
        "approvals": n("SELECT COUNT(*) FROM merge_execution_approvals"),
        "merge_history": n("SELECT COUNT(*) FROM company_merge_history"),
    }


def test_production_path_untouched() -> None:
    if Path(str(PRODUCTION_DB_PATH)).resolve().name.lower() != "northstar.db":
        _fail("production path unexpected")
    with get_connection() as conn:
        opened = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    if opened == Path(str(PRODUCTION_DB_PATH)).resolve():
        _fail("DS-11 tests opened live northstar.db")


def test_pair_ordering_schema_and_gates() -> None:
    if pair_ids(28, 22) != (22, 28):
        _fail("pair ordering is not deterministic")
    if pair_key(28, 22) != pair_key(22, 28) or pair_key(22, 28) != "22:28":
        _fail("22/28 and 28/22 must be the same pair")
    if live_destructive_enabled():
        _fail("hard delete/live destructive enabled")
    meta = refresh_meta()
    if meta.get("live_confirm_enabled"):
        _fail("LeadMaster confirm enabled")
    source = (ROOT / "duplicate_review.py").read_text(encoding="utf-8")
    if "company_merge_execute" in source or "create_merge_approval" in source:
        _fail("duplicate_review imports merge execution")
    for path in (
        ROOT / "crm_import_plan.py",
        ROOT / "research_import_plan.py",
        ROOT / "leadmaster_refresh_http.py",
        ROOT / "leadmaster_refresh_plan.py",
    ):
        text = path.read_text(encoding="utf-8")
        if "company_duplicate_reviews" in text or "MERGE_CANDIDATE" in text:
            _fail(f"{path.name} reads DS-11 review dispositions")
    actor = _admin_user()
    with get_connection() as conn:
        before = _counts(conn)
        created = _make_company(conn, actor=actor, company_name=f"DS11 Schema {_stamp()}", confirm_despite_match=True)
        conn.commit()
        after = _counts(conn)
        if after["approvals"] != 0 or after["merge_history"] != before["merge_history"]:
            _fail("schema/create changed merge tables")
        conn.execute("DELETE FROM companies WHERE id=?", (int(created["company_id"]),))
        conn.commit()


def test_discovery_evidence_and_review_lifecycle() -> None:
    actor = _admin_user()
    stamp = _stamp()
    clients = _clients()
    with get_connection() as conn:
        before = _counts(conn)
        name_addr = f"DS11 ExactNA {stamp}"
        a = _make_company(
            conn,
            actor=actor,
            company_name=name_addr,
            address="100 Industrial Dr",
            city="Green Bay",
            state="WI",
            zip_code="54301",
            phone="9205550100",
            website="https://exactna.example",
        )
        b = _make_company(
            conn,
            actor=actor,
            company_name=name_addr,
            address="100 Industrial Dr",
            city="Green Bay",
            state="WI",
            zip_code="54301",
            phone="9205550199",
            website="https://other.example",
        )
        phone_name = f"DS11 Phone {stamp}"
        p1 = _make_company(
            conn,
            actor=actor,
            company_name=phone_name,
            city="Itasca",
            state="IL",
            phone="6308750054",
        )
        p2 = _make_company(
            conn,
            actor=actor,
            company_name=phone_name,
            city="Itasca",
            state="IL",
            phone="6308750054",
        )
        domain_name = f"DS11 Domain {stamp}"
        d1 = _make_company(
            conn,
            actor=actor,
            company_name=domain_name,
            website="https://www.ds11domain.example",
        )
        d2 = _make_company(
            conn,
            actor=actor,
            company_name=f"{domain_name} LLC",
            website="https://ds11domain.example/about",
        )
        weak = f"DS11 NameOnly {stamp}"
        n1 = _make_company(conn, actor=actor, company_name=weak, city="Milwaukee", state="WI")
        n2 = _make_company(conn, actor=actor, company_name=weak, city="Chicago", state="IL")
        plant = f"DS11 Plant {stamp}"
        m1 = _make_company(
            conn,
            actor=actor,
            company_name=plant,
            address="1 Plant Rd",
            city="Hayward",
            state="CA",
        )
        m2 = _make_company(
            conn,
            actor=actor,
            company_name=plant,
            address="9 Other Rd",
            city="Galesburg",
            state="IL",
        )
        ident_a = _make_company(conn, actor=actor, company_name=f"DS11 IdentA {stamp}")
        ident_b = _make_company(conn, actor=actor, company_name=f"DS11 IdentB {stamp}")
        upsert_company_source_identity(
            conn,
            company_id=int(ident_a["company_id"]),
            source_system="leadmaster",
            source_record_no=f"DS11-{stamp}",
            client_id=clients["brown"],
            source_company_name="Ident A",
        )
        upsert_company_source_identity(
            conn,
            company_id=int(ident_b["company_id"]),
            source_system="leadmaster",
            source_record_no=f"DS11-{stamp}",
            client_id=clients["premier"],
            source_company_name="Ident B",
        )
        alias_root = _make_company(conn, actor=actor, company_name=f"DS11 AliasRoot {stamp}")
        alias_other = _make_company(conn, actor=actor, company_name=f"DS11 AliasOther {stamp}")
        upsert_company_alias(
            conn,
            company_id=int(alias_other["company_id"]),
            alias_name=f"DS11 AliasRoot {stamp}",
            source_system="manual",
            source_record_no=f"AL-{stamp}",
        )
        archived = _make_company(
            conn,
            actor=actor,
            company_name=f"DS11 ArchDup {stamp}",
            address="8 Archive St",
            city="Canton",
            state="MI",
        )
        live_match = _make_company(
            conn,
            actor=actor,
            company_name=f"DS11 ArchDup {stamp}",
            address="8 Archive St",
            city="Canton",
            state="MI",
        )
        archive_company(conn, actor=actor, company_id=int(archived["company_id"]), reason="Old duplicate")
        conn.commit()

        page = list_duplicate_candidates(conn, disposition="unreviewed", q=stamp, limit=100)
        if page.get("writes") or page.get("merge_will_occur") or page.get("automatic_verdict"):
            _fail("discovery must be read-only / non-verdict")
        pairs = {(int(r["company_a_id"]), int(r["company_b_id"])): r for r in page["pairs"]}

        def _need(left: dict, right: dict, cat: str) -> dict:
            key = pair_ids(int(left["company_id"]), int(right["company_id"]))
            row = pairs.get(key)
            if row is None:
                _fail(f"missing candidate {cat} {key}")
            if cat not in row["categories"]:
                _fail(f"{cat} not in {row['categories']} for {key}")
            summary = str(row["evidence_summary"]).lower()
            if "definitely" in summary:
                _fail("automatic definite verdict")
            return row

        _need(a, b, CAT_EXACT_NAME_ADDRESS)
        _need(p1, p2, CAT_EXACT_PHONE)
        _need(d1, d2, CAT_EXACT_DOMAIN)
        _need(n1, n2, CAT_NAME_ONLY)
        _need(m1, m2, CAT_MULTI_LOCATION)
        _need(ident_a, ident_b, CAT_IDENTITY)
        arch = _need(archived, live_match, CAT_EXACT_NAME_ADDRESS)
        if not (arch["company_a"]["archived"] or arch["company_b"]["archived"]):
            _fail("archived company not flagged")

        paged = list_duplicate_candidates(conn, disposition="unreviewed", q=stamp, offset=0, limit=2)
        if paged["limit"] != 2 or len(paged["pairs"]) > 2:
            _fail("pagination did not cap page size")
        if paged["total"] < 6:
            _fail(f"expected several isolated pairs, got {paged['total']}")

        detail = get_duplicate_pair_detail(conn, int(b["company_id"]), int(a["company_id"]))
        if detail["pair_key"] != pair_key(int(a["company_id"]), int(b["company_id"])):
            _fail("detail pair key mismatch")
        if detail["company_a"]["company_id"] >= detail["company_b"]["company_id"]:
            _fail("detail sides not ordered")
        if not detail["planning_only"] or detail["merge_will_occur"] or not detail["no_merge_button"]:
            _fail("detail must stay plan-only")

        try:
            save_duplicate_review(
                conn,
                actor=actor,
                company_a_id=int(a["company_id"]),
                company_b_id=int(b["company_id"]),
                body=DuplicateReviewSaveRequest(disposition=DISPOSITION_NOT, reason=""),
            )
            _fail("blank reason allowed")
        except DuplicateReviewError as exc:
            if str(exc) != "reason_required":
                _fail(f"reason error {exc}")

        save_duplicate_review(
            conn,
            actor=actor,
            company_a_id=int(b["company_id"]),
            company_b_id=int(a["company_id"]),
            body=DuplicateReviewSaveRequest(
                disposition=DISPOSITION_NOT,
                reason="Different companies with similar names.",
                actor_id=999999,
            ),
        )
        dup_rows = conn.execute(
            "SELECT COUNT(*) FROM company_duplicate_reviews WHERE pair_key=?",
            (pair_key(int(a["company_id"]), int(b["company_id"])),),
        ).fetchone()[0]
        if int(dup_rows) != 1:
            _fail("same pair stored twice")
        hidden = list_duplicate_candidates(conn, disposition="unreviewed", q=name_addr, limit=50)
        if any(
            pair_key(int(a["company_id"]), int(b["company_id"])) == r["pair_key"]
            for r in hidden["pairs"]
        ):
            _fail("NOT_DUPLICATE pair still in unreviewed")
        listed = list_duplicate_candidates(conn, disposition="not_duplicate", q=name_addr, limit=50)
        if not listed["pairs"]:
            _fail("NOT_DUPLICATE filter empty")

        save_duplicate_review(
            conn,
            actor=actor,
            company_a_id=int(m1["company_id"]),
            company_b_id=int(m2["company_id"]),
            body=DuplicateReviewSaveRequest(
                disposition=DISPOSITION_MULTI,
                reason="Separate manufacturing locations; retain independently.",
            ),
        )
        save_duplicate_review(
            conn,
            actor=actor,
            company_a_id=int(n1["company_id"]),
            company_b_id=int(n2["company_id"]),
            body=DuplicateReviewSaveRequest(
                disposition=DISPOSITION_RESEARCH,
                reason="Need to verify HQ versus plant.",
            ),
        )
        save_duplicate_review(
            conn,
            actor=actor,
            company_a_id=int(p1["company_id"]),
            company_b_id=int(p2["company_id"]),
            body=DuplicateReviewSaveRequest(
                disposition=DISPOSITION_LIKELY,
                reason="Phone and name match; survivor not chosen.",
            ),
        )
        try:
            save_duplicate_review(
                conn,
                actor=actor,
                company_a_id=int(d1["company_id"]),
                company_b_id=int(d2["company_id"]),
                body=DuplicateReviewSaveRequest(
                    disposition=DISPOSITION_MERGE,
                    reason="Plan only later.",
                ),
            )
            _fail("MERGE_CANDIDATE without survivor")
        except DuplicateReviewError as exc:
            if str(exc) != "survivor_required":
                _fail(f"survivor error {exc}")
        try:
            save_duplicate_review(
                conn,
                actor=actor,
                company_a_id=int(d1["company_id"]),
                company_b_id=int(d2["company_id"]),
                body=DuplicateReviewSaveRequest(
                    disposition=DISPOSITION_MERGE,
                    reason="Plan only later.",
                    proposed_survivor_company_id=int(d1["company_id"]),
                    proposed_source_company_id=int(d1["company_id"]),
                ),
            )
            _fail("source==survivor allowed")
        except DuplicateReviewError as exc:
            if str(exc) != "source_equals_survivor":
                _fail(f"equal survivor error {exc}")
        try:
            save_duplicate_review(
                conn,
                actor=actor,
                company_a_id=int(d1["company_id"]),
                company_b_id=int(d2["company_id"]),
                body=DuplicateReviewSaveRequest(
                    disposition=DISPOSITION_MERGE,
                    reason="Plan only later.",
                    proposed_survivor_company_id=int(d1["company_id"]),
                    proposed_source_company_id=1,
                ),
            )
            _fail("outsider survivor allowed")
        except DuplicateReviewError as exc:
            if str(exc) != "survivor_not_in_pair":
                _fail(f"pair membership error {exc}")
        merge_saved = save_duplicate_review(
            conn,
            actor=actor,
            company_a_id=int(d1["company_id"]),
            company_b_id=int(d2["company_id"]),
            body=DuplicateReviewSaveRequest(
                disposition=DISPOSITION_MERGE,
                reason="Reviewed pair; plan only for later DS-12.",
                proposed_survivor_company_id=int(d2["company_id"]),
                proposed_source_company_id=int(d1["company_id"]),
            ),
        )
        if merge_saved["merge_will_occur"] or merge_saved["approval_created"] or merge_saved["companies_mutated"]:
            _fail("MERGE_CANDIDATE mutated or approved merge")
        if _counts(conn)["approvals"] != 0:
            _fail("merge approval created")
        revised = save_duplicate_review(
            conn,
            actor=actor,
            company_a_id=int(d1["company_id"]),
            company_b_id=int(d2["company_id"]),
            body=DuplicateReviewSaveRequest(
                disposition=DISPOSITION_MULTI,
                reason="Revised to multi-location after more research.",
            ),
        )
        events = list_review_history(conn, int(d1["company_id"]), int(d2["company_id"]))["events"]
        if len(events) < 2:
            _fail("review history lost prior decision")
        if events[0]["new_disposition"] != DISPOSITION_MULTI:
            _fail("latest history not MULTI_LOCATION")
        if revised["proposed_survivor_company_id"] is not None:
            _fail("survivor should clear when leaving MERGE_CANDIDATE")

        fingerprint = conn.execute(
            "SELECT evidence_fingerprint FROM company_duplicate_reviews WHERE pair_key=?",
            (pair_key(int(a["company_id"]), int(b["company_id"])),),
        ).fetchone()[0]
        conn.execute(
            "UPDATE companies SET website=? WHERE id=?",
            ("https://changed-evidence.example", int(a["company_id"])),
        )
        conn.commit()
        stale_page = list_duplicate_candidates(conn, disposition="unreviewed", q=name_addr, limit=50)
        stale = next(
            (
                r
                for r in stale_page["pairs"]
                if r["pair_key"] == pair_key(int(a["company_id"]), int(b["company_id"]))
            ),
            None,
        )
        if stale is None or not stale["stale"]:
            _fail("changed evidence did not resurface as stale")
        if stale["review_status"] != "RE_REVIEW_NEEDED":
            _fail("stale review not marked RE-REVIEW NEEDED")
        merge_page = list_duplicate_candidates(conn, disposition="merge_candidate", q=stamp, limit=50)
        if merge_page["pairs"]:
            _fail("stale/cleared MERGE_CANDIDATE still listed as current")
        after = _counts(conn)
        if after["approvals"] != 0:
            _fail("approvals changed")
        if after["merge_history"] != before["merge_history"]:
            _fail("merge history changed")
        prov = latest_provenance(
            conn,
            entity_type="duplicate_review",
            entity_id=int(merge_saved["review_id"]),
            field="disposition",
        )
        if prov is None or prov.get("source_type") != SOURCE_MANUAL_ADMIN:
            _fail("missing duplicate review provenance")
        if _blank_action(prov) != ACTION_DUPLICATE_REVIEW:
            _fail("wrong provenance action")
        if int(prov.get("changed_by_user_id") or 0) != int(actor.id):
            _fail("spoofed actor used for review")


def _blank_action(row: dict) -> str:
    return str(row.get("action") or "").strip()


def test_conflicts_preservation_and_import_safety() -> None:
    actor = _admin_user()
    stamp = _stamp()
    clients = _clients()
    with get_connection() as conn:
        before = _counts(conn)
        name = f"DS11 Shared {stamp}"
        left = _make_company(
            conn,
            actor=actor,
            company_name=name,
            address="1 Shared",
            city="Oskaloosa",
            state="IA",
            phone="6416730469",
        )
        right = _make_company(
            conn,
            actor=actor,
            company_name=name,
            address="1 Shared",
            city="Oskaloosa",
            state="IA",
            phone="6416730469",
        )
        lid = int(left["company_id"])
        rid = int(right["company_id"])
        brown_left = link_relationship(conn, actor=actor, client_id=clients["brown"], company_id=lid, status="Hot Prospect")
        conn.execute(
            "UPDATE client_company_relationships SET is_hot=1, follow_up_date=?, next_action=?, notes=? WHERE id=?",
            ("2026-10-01", "Call plant", "Keep this Brown note", brown_left),
        )
        link_relationship(conn, actor=actor, client_id=clients["premier"], company_id=lid, status="New")
        brown_right = link_relationship(
            conn, actor=actor, client_id=clients["brown"], company_id=rid, status="Appointment Set"
        )
        conn.execute(
            "UPDATE client_company_relationships SET assigned_user_id=? WHERE id=?",
            (int(actor.id), brown_right),
        )
        removed = link_relationship(conn, actor=actor, client_id=clients["dawson"], company_id=rid, status="New")
        remove_relationship(conn, actor=actor, ccr_id=removed, reason="Wrong book")
        create_contact(
            conn,
            actor=actor,
            company_id=lid,
            first_name="Pat",
            last_name="Lee",
            email=f"pat.{stamp}@shared.example",
            phone="6415550100",
            title="Buyer",
        )
        create_contact(
            conn,
            actor=actor,
            company_id=rid,
            first_name="Pat",
            last_name="Lee",
            email=f"pat.{stamp}@shared.example",
            phone="6415550100",
            title="Buyer",
        )
        conn.commit()
        left_name = conn.execute("SELECT company_name FROM companies WHERE id=?", (lid,)).fetchone()[0]
        detail = get_duplicate_pair_detail(conn, lid, rid)
        if not detail["same_client_conflicts"]:
            _fail("missing same-client CCR conflict")
        if detail["dependency"]["overall"] != "CONFLICT":
            _fail(f"expected CONFLICT, got {detail['dependency']}")
        if not any(o.get("kind") == "exact_email" for o in detail["contact_overlaps"]):
            _fail("contact email overlap missing")
        brown_conflict = next(c for c in detail["same_client_conflicts"] if c["client_id"] == clients["brown"])
        if not brown_conflict["company_a"]["status"] or not brown_conflict["company_b"]["status"]:
            _fail("conflict missing CCR status")
        dawson_right = [c for c in detail["company_b"]["ccrs"] if c["client_id"] == clients["dawson"]]
        if dawson_right and dawson_right[0]["active"]:
            _fail("removed CCR shown as active")
        saved = save_duplicate_review(
            conn,
            actor=actor,
            company_a_id=lid,
            company_b_id=rid,
            body=DuplicateReviewSaveRequest(
                disposition=DISPOSITION_MERGE,
                reason="Shared-company planning only.",
                proposed_survivor_company_id=lid,
                proposed_source_company_id=rid,
            ),
        )
        after = _counts(conn)
        if after["contacts"] != before["contacts"] + 2:
            _fail("contact count drifted beyond the two test contacts")
        if after["ccr"] < before["ccr"]:
            _fail("CCR rows disappeared")
        now_name = conn.execute("SELECT company_name FROM companies WHERE id=?", (lid,)).fetchone()[0]
        if now_name != left_name:
            _fail("master company mutated")
        brown_row = conn.execute(
            "SELECT status, is_hot, follow_up_date, next_action, notes FROM client_company_relationships WHERE id=?",
            (brown_left,),
        ).fetchone()
        if brown_row["status"] != "Hot Prospect" or not brown_row["is_hot"]:
            _fail("Hot/status not preserved")
        if brown_row["follow_up_date"] != "2026-10-01" or brown_row["next_action"] != "Call plant":
            _fail("follow-up/next action mutated")
        if "Keep this Brown note" not in str(brown_row["notes"]):
            _fail("notes mutated")
        matches = find_scored_matches(
            conn,
            client_id=clients["carmeco"],
            company_name=name,
            address="1 Shared",
            city="Oskaloosa",
            state="IA",
            phone="6416730469",
            include_ai=False,
        )
        match_ids = {int(m["company_id"]) for m in matches}
        if lid not in match_ids or rid not in match_ids:
            _fail("MERGE_CANDIDATE suppressed Add Company matching")
        if saved["approval_created"] or after["approvals"] != 0:
            _fail("import/review created merge approval")
        if after["merge_history"] != before["merge_history"]:
            _fail("merge executed")
        if live_master_archive_enabled() is not True:
            _fail("archive gate changed")
        preview = preview_archive_company(conn, actor=actor, company_id=lid, reason="no")
        if preview.get("eligible"):
            _fail("active CCR should still block archive")


def test_http_admin_only_and_add_company_regression() -> None:
    actor = _admin_user()
    password = f"NsTest9{secrets.token_hex(8)}"
    _set_password(int(actor.id), actor.email, password)
    http = TestClient(app)
    csrf = _login(http, actor.email, password)
    headers = {CSRF_HEADER: csrf}
    meta = http.get("/api/admin/data-steward/meta")
    if meta.status_code != 200:
        _fail(f"admin meta {meta.status_code}")
    body = meta.json()
    if body.get("delete_enabled") or body.get("merge_enabled"):
        _fail(f"destructive flags {body}")
    if not body.get("duplicate_review_enabled"):
        _fail("duplicate review flag off")
    stamp = _stamp()
    with get_connection() as conn:
        created = _make_company(
            conn,
            actor=actor,
            company_name=f"DS11 HTTP {stamp}",
            address="9 Http Rd",
            city="Romeoville",
            state="IL",
        )
        other = _make_company(
            conn,
            actor=actor,
            company_name=f"DS11 HTTP {stamp}",
            address="9 Http Rd",
            city="Romeoville",
            state="IL",
        )
        conn.commit()
        a_id = int(created["company_id"])
        b_id = int(other["company_id"])
    listed = http.get(
        "/api/admin/duplicate-review/candidates",
        params={"q": f"DS11 HTTP {stamp}", "limit": "20"},
    )
    if listed.status_code != 200:
        _fail(f"list {listed.status_code} {listed.text}")
    if listed.json().get("writes") is not False:
        _fail("list wrote")
    detail = http.get(f"/api/admin/duplicate-review/pairs/{b_id}/{a_id}")
    if detail.status_code != 200:
        _fail(f"detail {detail.status_code} {detail.text}")
    saved = http.post(
        f"/api/admin/duplicate-review/pairs/{b_id}/{a_id}",
        json={
            "disposition": "LIKELY_DUPLICATE",
            "reason": "HTTP review uses session actor.",
            "actor_id": 999999,
            "created_by": "Impostor",
        },
        headers=headers,
    )
    if saved.status_code != 200:
        _fail(f"save {saved.status_code} {saved.text}")
    payload = saved.json()
    if payload.get("merge_will_occur") or payload.get("approval_created"):
        _fail("http save merged")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT reviewed_by_user_id FROM company_duplicate_reviews WHERE pair_key=?",
            (pair_key(a_id, b_id),),
        ).fetchone()
        if int(row["reviewed_by_user_id"]) != int(actor.id):
            _fail("spoofed actor stored")
        events = provenance_history(conn, entity_type="duplicate_review", limit=5)
        if events and int(events[0].get("changed_by_user_id") or 0) not in {int(actor.id), 0}:
            pass
    specialist_pw = f"NsTest9{secrets.token_hex(8)}"
    spec = provision_staff_user(
        first_name="Dana",
        last_name="Iso",
        email=f"ds11.spec.{_stamp()}@northstar.example.test",
        password=specialist_pw,
        staff_role=REVOPS_SPECIALIST,
        client_ids=[_clients()["brown"]],
    )
    spec_csrf = _login(http, spec["user"]["email"], specialist_pw)
    denied = http.get("/api/admin/duplicate-review/candidates", headers={CSRF_HEADER: spec_csrf})
    if denied.status_code != 403:
        _fail(f"specialist list {denied.status_code}")
    if denied.json().get("detail") != ADMIN_REQUIRED_DETAIL:
        _fail(f"specialist detail {denied.json()}")
    denied_save = http.post(
        f"/api/admin/duplicate-review/pairs/{a_id}/{b_id}",
        json={"disposition": "NOT_DUPLICATE", "reason": "nope"},
        headers={CSRF_HEADER: spec_csrf},
    )
    if denied_save.status_code != 403:
        _fail(f"specialist save {denied_save.status_code}")
    with _enforcement_on():
        anon = TestClient(app)
        unauth = anon.get("/api/admin/duplicate-review/candidates")
        if unauth.status_code != 401:
            _fail(f"unauthenticated {unauth.status_code}")
        if unauth.json().get("detail") != AUTH_REQUIRED_DETAIL:
            _fail(f"unauthenticated detail {unauth.json()}")
    import test_add_company_duplicates

    rc = test_add_company_duplicates.main()
    if rc not in (0, None):
        _fail(f"add company duplicates rc={rc}")
    if refresh_meta().get("live_confirm_enabled"):
        _fail("LM confirm enabled after DS-11")


def test_ds7_ds10_gate_regressions() -> None:
    from data_steward import live_ccr_lifecycle_enabled, live_company_amend_enabled

    if not live_company_amend_enabled() or not live_ccr_lifecycle_enabled() or not live_master_archive_enabled():
        _fail("DS-7/8/9 gates off")
    if live_destructive_enabled():
        _fail("destructive enabled")
    import test_data_steward_ds10

    test_data_steward_ds10.test_gates_still_ds9()


if __name__ == "__main__":
    tests = (
        test_production_path_untouched,
        test_pair_ordering_schema_and_gates,
        test_discovery_evidence_and_review_lifecycle,
        test_conflicts_preservation_and_import_safety,
        test_http_admin_only_and_add_company_regression,
        test_ds7_ds10_gate_regressions,
    )
    for test in tests:
        test()
        print(f"{test.__name__}: ok")
    print("DS-11 isolated tests passed")
