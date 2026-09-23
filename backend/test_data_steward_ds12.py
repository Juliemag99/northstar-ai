"""DS-12 automated duplicate classification and batch review.

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
    ACTION_DUPLICATE_BATCH_REVIEW,
    SOURCE_MANUAL_ADMIN,
    archive_company,
    create_company,
    create_contact,
    link_relationship,
    live_destructive_enabled,
    provenance_history,
)
from db import PRODUCTION_DB_PATH, get_connection
from duplicate_classify import (
    BATCH_ACCEPT_LIKELY,
    CLASS_HIGH,
    CLASS_HUMAN,
    CLASS_INSUFFICIENT,
    CLASS_LIKELY,
    CLASS_MULTI,
    CLASS_NOT,
    CLASSIFIER_VERSION,
    analyze_duplicate_candidates,
    classify_from_signals,
    classify_pair_readonly,
    confirm_batch_review,
    DuplicateBatchReviewRequest,
    load_classifications,
    maybe_second_opinion,
    preview_batch_review,
    propose_survivor,
)
from duplicate_review import (
    DISPOSITION_LIKELY,
    DISPOSITION_MERGE,
    DuplicateReviewError,
    DuplicateReviewSaveRequest,
    ensure_duplicate_review_schema,
    evidence_fingerprint,
    list_duplicate_candidates,
    pair_key,
    save_duplicate_review,
)
from leadmaster_refresh_http import refresh_meta
from main import app
from models import NorthStarUser
from staff_provisioning import provision_staff_user
from staff_rbac import REVOPS_SPECIALIST

ROOT = Path(__file__).resolve().parent
KNOWN_DUPES = (
    (22, 28, "EDL"),
    (45, 97, "Kelderman"),
    (151, 212, "BW"),
    (211, 226, "Spee-Dee"),
    (229, 358, "Kuhn"),
    (550, 591, "Tech Max"),
    (558, 606, "Parsons"),
)
KNOWN_MULTI = (
    (215, 383, "Greenheck"),
    (296, 348, "HEICO"),
    (336, 337, "Watlow 336/337"),
    (336, 670, "Watlow 336/670"),
    (337, 670, "Watlow 337/670"),
    (85, 192, "Parker"),
    (580, 618, "Seneca"),
    (156, 158, "Heat & Control"),
    (754, 755, "B&W 754/755"),
    (754, 756, "B&W 754/756"),
    (755, 756, "B&W 755/756"),
)
KNOWN_REVIEW = ((149, 753, "Baltimore Aircoil"), (551, 757, "Johnson"))


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
        "reviews": n("SELECT COUNT(*) FROM company_duplicate_reviews")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_duplicate_reviews'").fetchone()
        else 0,
        "classifications": n("SELECT COUNT(*) FROM company_duplicate_classifications")
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_duplicate_classifications'"
        ).fetchone()
        else 0,
        "campaigns": n("SELECT COUNT(*) FROM campaign_companies")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_companies'").fetchone()
        else 0,
        "provenance": n("SELECT COUNT(*) FROM field_provenance_events"),
    }


def test_production_path_untouched() -> None:
    if Path(str(PRODUCTION_DB_PATH)).resolve().name.lower() != "northstar.db":
        _fail("production path unexpected")
    with get_connection() as conn:
        opened = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    if opened == Path(str(PRODUCTION_DB_PATH)).resolve():
        _fail("DS-12 tests opened live northstar.db")


def test_pure_classifier_buckets() -> None:
    if maybe_second_opinion({"pair": "1:2"}, provider=None) is not None:
        _fail("DS-12 called a second-opinion provider")
    high = classify_from_signals(
        {
            "canonical_equal": True,
            "addr_equal": True,
            "city_state": True,
            "phone_equal": True,
            "domain_equal": True,
            "same_client_ccr": True,
            "material_ccr_conflict": True,
        }
    )
    if high["classification"] != CLASS_HIGH:
        _fail(f"name+address should be HIGH, got {high['classification']}")
    if high["confidence"] != "HIGH":
        _fail("HIGH confidence missing")
    if not high["same_client_ccr_conflict"]:
        _fail("CCR conflict should remain a concern, not identity proof")
    if high["opaque_score"] or high["external_ai_used"]:
        _fail("opaque/external AI flags set")
    if "Canonical names normalize identically" not in high["why"]:
        _fail("HIGH missing name fact")
    likely = classify_from_signals(
        {"canonical_equal": True, "phone_equal": True, "city_state": True}
    )
    if likely["classification"] not in {CLASS_HIGH, CLASS_LIKELY}:
        _fail(f"name+phone should be duplicate-ish, got {likely['classification']}")
    multi = classify_from_signals(
        {
            "canonical_equal": True,
            "domain_equal": True,
            "different_city": True,
            "addr_equal": False,
        }
    )
    if multi["classification"] != CLASS_MULTI:
        _fail(f"same name/domain different city should be MULTI, got {multi['classification']}")
    not_dup = classify_from_signals(
        {
            "canonical_equal": False,
            "alias_match": False,
            "addr_equal": False,
            "phone_equal": False,
            "different_domain": True,
            "different_phone": True,
            "different_city": True,
            "both_have_domain": True,
            "both_have_phone": True,
        }
    )
    if not_dup["classification"] != CLASS_NOT:
        _fail(f"independent differences should be NOT, got {not_dup['classification']}")
    weak = classify_from_signals({"core_only": True, "name_equal": True})
    if weak["classification"] != CLASS_INSUFFICIENT:
        _fail(f"core-name-only should be INSUFFICIENT, got {weak['classification']}")
    human = classify_from_signals(
        {
            "canonical_equal": True,
            "different_city": True,
            "addr_equal": False,
            "identity_conflict": True,
        }
    )
    if human["classification"] != CLASS_HUMAN:
        _fail(f"identity conflict should force HUMAN, got {human['classification']}")
    name_only_cities = classify_from_signals(
        {"canonical_equal": True, "different_city": True, "addr_equal": False}
    )
    if name_only_cities["classification"] != CLASS_HUMAN:
        _fail(f"name-only different cities should be HUMAN, got {name_only_cities['classification']}")
    archived = classify_from_signals({"archived_any": True, "core_only": True})
    if archived["classification"] != CLASS_HUMAN:
        _fail(f"archived unclear identity should be HUMAN, got {archived['classification']}")
    one_diff = classify_from_signals(
        {"canonical_equal": True, "addr_equal": True, "different_phone": True}
    )
    if one_diff["classification"] == CLASS_NOT:
        _fail("one differing field must not yield LIKELY_NOT_DUPLICATE")
    ambiguous = propose_survivor(
        {
            "company_id": 10,
            "has_external_rn": True,
            "active_ccr_count": 1,
            "client_count": 1,
            "contact_count": 4,
            "has_canonical_address": True,
        },
        {
            "company_id": 2,
            "has_external_rn": True,
            "active_ccr_count": 1,
            "client_count": 1,
            "contact_count": 5,
            "has_canonical_address": True,
        },
    )
    if ambiguous["proposed_survivor_company_id"] is not None:
        _fail("ambiguous survivor used a preference")
    if not ambiguous["survivor_decision_required"] or ambiguous["used_lower_id_tiebreak"]:
        _fail("survivor ambiguity mishandled")
    clear = propose_survivor(
        {
            "company_id": 99,
            "has_external_rn": True,
            "client_count": 3,
            "identity_count": 3,
            "has_canonical_address": True,
            "history_activity_count": 8,
        },
        {"company_id": 1, "contact_count": 40},
    )
    if clear["proposed_survivor_company_id"] != 99:
        _fail("survivor picked lower id or contact-count winner")


def test_schema_analyze_idempotence_and_authority() -> None:
    actor = _admin_user()
    stamp = _stamp()
    clients = _clients()
    with get_connection() as conn:
        ensure_duplicate_review_schema(conn)
        before = _counts(conn)
        edl_before = conn.execute(
            "SELECT disposition FROM company_duplicate_reviews WHERE pair_key='22:28'"
        ).fetchone()
        name = f"DS12 High {stamp}"
        left = _make_company(
            conn,
            actor=actor,
            company_name=name,
            address="1 Classification Way",
            city="Green Bay",
            state="WI",
            zip_code="54301",
            phone="9205551001",
            website="https://ds12high.example",
        )
        right = _make_company(
            conn,
            actor=actor,
            company_name=name,
            address="1 Classification Way",
            city="Green Bay",
            state="WI",
            zip_code="54301",
            phone="9205551001",
            website="https://ds12high.example",
        )
        plant = f"DS12 Plant {stamp}"
        m1 = _make_company(
            conn,
            actor=actor,
            company_name=plant,
            address="10 Plant Rd",
            city="Schofield",
            state="WI",
            website="https://ds12plant.example",
        )
        m2 = _make_company(
            conn,
            actor=actor,
            company_name=plant,
            address="90 Other Ave",
            city="Tulsa",
            state="OK",
            website="https://ds12plant.example",
        )
        weak = f"DS12 Weak {stamp}"
        w1 = _make_company(conn, actor=actor, company_name=weak)
        w2 = _make_company(conn, actor=actor, company_name=weak)
        ident_name = f"DS12 Ident {stamp}"
        i1 = _make_company(conn, actor=actor, company_name=ident_name, city="Ames", state="IA")
        i2 = _make_company(conn, actor=actor, company_name=f"{ident_name} Co", city="Des Moines", state="IA")
        upsert_company_source_identity(
            conn,
            company_id=int(i1["company_id"]),
            source_system="leadmaster",
            source_record_no=f"RN-{stamp}",
            source_company_name=ident_name,
        )
        upsert_company_source_identity(
            conn,
            company_id=int(i2["company_id"]),
            source_system="leadmaster",
            source_record_no=f"RN-{stamp}",
            source_company_name=ident_name,
        )
        arch = f"DS12 Arch {stamp}"
        a1 = _make_company(conn, actor=actor, company_name=arch, city="Omaha", state="NE")
        a2 = _make_company(conn, actor=actor, company_name=arch, city="Lincoln", state="NE")
        archive_company(conn, actor=actor, company_id=int(a2["company_id"]), reason="Archive for DS-12 classification.")
        lid = int(left["company_id"])
        rid = int(right["company_id"])
        link_relationship(conn, actor=actor, client_id=clients["carmeco"], company_id=lid)
        link_relationship(conn, actor=actor, client_id=clients["carmeco"], company_id=rid)
        create_contact(
            conn,
            actor=actor,
            company_id=lid,
            first_name="Pat",
            last_name="Lee",
            email=f"pat.{stamp}@ds12.example",
        )
        create_contact(
            conn,
            actor=actor,
            company_id=rid,
            first_name="Pat",
            last_name="Lee",
            email=f"pat.{stamp}@ds12.example",
        )
        upsert_company_alias(conn, company_id=lid, alias_name=f"{name} Alias", source_system="manual")
        conn.commit()
        first = analyze_duplicate_candidates(conn, actor=actor)
        second = analyze_duplicate_candidates(conn, actor=actor)
        after = _counts(conn)
        if first["human_reviews_mutated"] or second["human_reviews_mutated"]:
            _fail("analyze mutated human reviews")
        if after["companies"] < before["companies"] or after["ccr"] < before["ccr"]:
            _fail("analyze mutated business rows downward")
        if after["approvals"] != 0:
            _fail("classification created merge approval")
        if after["merge_history"] != before["merge_history"]:
            _fail("classification executed merge")
        if second["candidates_analyzed"] != first["candidates_analyzed"]:
            _fail("rerun candidate count drifted")
        if after["classifications"] < first["candidates_analyzed"]:
            _fail("classification rows missing")
        if first["classifier_version"] != CLASSIFIER_VERSION:
            _fail("classifier version missing")
        high_pair = classify_pair_readonly(conn, lid, rid)
        if high_pair["classification"] != CLASS_HIGH:
            _fail(f"synthetic high pair {high_pair['classification']}")
        if not high_pair["same_client_ccr_conflict"]:
            _fail("synthetic CCR conflict dropped")
        multi_pair = classify_pair_readonly(conn, int(m1["company_id"]), int(m2["company_id"]))
        if multi_pair["classification"] != CLASS_MULTI:
            _fail(f"synthetic multi {multi_pair['classification']}")
        ident_pair = classify_pair_readonly(conn, int(i1["company_id"]), int(i2["company_id"]))
        if ident_pair["classification"] != CLASS_HUMAN:
            _fail(f"identity conflict {ident_pair['classification']}")
        weak_pair = classify_pair_readonly(conn, int(w1["company_id"]), int(w2["company_id"]))
        if weak_pair["classification"] not in {CLASS_INSUFFICIENT, CLASS_HUMAN}:
            _fail(f"weak name-only {weak_pair['classification']}")
        arch_pair = classify_pair_readonly(conn, int(a1["company_id"]), int(a2["company_id"]))
        if arch_pair["classification"] not in {CLASS_HUMAN, CLASS_INSUFFICIENT, CLASS_MULTI}:
            _fail(f"archived pair {arch_pair['classification']}")
        stored = load_classifications(conn).get(pair_key(lid, rid))
        if stored is None or stored["classifier_version"] != CLASSIFIER_VERSION:
            _fail("stored classification missing")
        fp = evidence_fingerprint(conn, lid, rid)
        if stored["evidence_fingerprint"] != fp:
            _fail("fingerprint not stored")
        conn.execute("UPDATE companies SET address=? WHERE id=?", ("999 Changed St", lid))
        conn.commit()
        listed = list_duplicate_candidates(conn, queue="high_confidence", q=name, limit=20)
        stale_rows = [p for p in listed["pairs"] if p["pair_key"] == pair_key(lid, rid)]
        needed = list_duplicate_candidates(conn, queue="review_needed", q=name, limit=50)
        if listed["summary"]["candidates"] < 1:
            _fail("summary missing candidates")
        edl_after = conn.execute(
            "SELECT disposition FROM company_duplicate_reviews WHERE pair_key='22:28'"
        ).fetchone()
        if (edl_before is None) != (edl_after is None):
            _fail("EDL human review created or deleted by analyze")
        if edl_before is not None and edl_after["disposition"] != edl_before["disposition"]:
            _fail("analyze overwrote Julie disposition")
        save_duplicate_review(
            conn,
            actor=actor,
            company_a_id=lid,
            company_b_id=rid,
            body=DuplicateReviewSaveRequest(disposition="MULTI_LOCATION", reason="Two plants, not a merge."),
        )
        listed2 = list_duplicate_candidates(conn, queue="disagreement", q=name, limit=20)
        if not any(p["disagreement"] for p in listed2["pairs"]):
            _fail("human/automation disagreement not surfaced")
        _ = stale_rows, needed
        preview = preview_batch_review(
            conn,
            actor=actor,
            body=DuplicateBatchReviewRequest(
                action=BATCH_ACCEPT_LIKELY,
                reason="Accept classified high-confidence synthetic pair.",
                selection_mode="explicit",
                pair_keys=[pair_key(int(m1["company_id"]), int(m2["company_id"]))],
            ),
        )
        if preview["writes"] or not preview["confirm_allowed"]:
            _fail("batch preview wrote or blocked eligible pair")
        confirmed = confirm_batch_review(
            conn,
            actor=actor,
            body=DuplicateBatchReviewRequest(
                action=BATCH_ACCEPT_LIKELY,
                reason="Accept classified high-confidence synthetic pair.",
                selection_mode="explicit",
                pair_keys=[pair_key(int(m1["company_id"]), int(m2["company_id"]))],
                preview_fingerprint=preview["preview_fingerprint"],
                confirm=True,
            ),
        )
        if confirmed["saved"] != 1 or confirmed["merge_will_occur"] or confirmed["approval_created"]:
            _fail("batch confirm merged or failed")
        event = conn.execute(
            """
            SELECT review_source, automated_classification, classifier_version, actor_user_id
            FROM company_duplicate_review_events
            WHERE pair_key=? ORDER BY id DESC LIMIT 1
            """,
            (pair_key(int(m1["company_id"]), int(m2["company_id"])),),
        ).fetchone()
        if event["review_source"] != "DUPLICATE_BATCH_REVIEW":
            _fail("batch source missing")
        if event["classifier_version"] != CLASSIFIER_VERSION:
            _fail("batch classifier version missing")
        if int(event["actor_user_id"]) != int(actor.id):
            _fail("batch actor spoofable")
        events = provenance_history(conn, entity_type="duplicate_review", limit=8)
        if not any(e.get("action") == ACTION_DUPLICATE_BATCH_REVIEW for e in events):
            _fail("batch provenance action missing")
        try:
            preview_batch_review(
                conn,
                actor=actor,
                body=DuplicateBatchReviewRequest(action="MERGE_CANDIDATE", reason="nope", pair_keys=["1:2"]),
            )
            _fail("batch MERGE_CANDIDATE allowed")
        except DuplicateReviewError as exc:
            if str(exc) != "batch_merge_candidate_forbidden":
                _fail(f"wrong merge-candidate error {exc}")
        try:
            confirm_batch_review(
                conn,
                actor=actor,
                body=DuplicateBatchReviewRequest(
                    action=BATCH_ACCEPT_LIKELY,
                    reason="no",
                    pair_keys=[pair_key(lid, rid)],
                    confirm=True,
                ),
            )
            _fail("short reason allowed")
        except DuplicateReviewError as exc:
            if str(exc) not in {"reason_required", "preview_stale", "nothing_eligible"}:
                _fail(f"unexpected reason error {exc}")
        matches = find_scored_matches(
            conn,
            client_id=clients["carmeco"],
            company_name=name,
            address="1 Classification Way",
            city="Green Bay",
            state="WI",
            include_ai=False,
        )
        match_ids = {int(m["company_id"]) for m in matches}
        if lid not in match_ids or rid not in match_ids:
            _fail("HIGH_CONFIDENCE suppressed Add Company matching")
        after2 = _counts(conn)
        if after2["approvals"] != 0 or after2["merge_history"] != before["merge_history"]:
            _fail("later DS-12 writes merged")


def test_http_admin_only_and_gates() -> None:
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
    if not body.get("duplicate_classification_enabled"):
        _fail("classification flag off")
    analyzed = http.post("/api/admin/duplicate-review/analyze", json={"actor_id": 999999, "created_by": "Impostor"}, headers=headers)
    if analyzed.status_code != 200:
        _fail(f"analyze {analyzed.status_code} {analyzed.text}")
    payload = analyzed.json()
    if payload.get("merge_will_occur") or payload.get("approval_created") or payload.get("human_reviews_mutated"):
        _fail("http analyze mutated human/merge state")
    listed = http.get("/api/admin/duplicate-review/candidates", params={"queue": "review_needed", "limit": "20"})
    if listed.status_code != 200 or listed.json().get("writes") is not False:
        _fail(f"review needed list {listed.status_code}")
    high = http.get("/api/admin/duplicate-review/candidates", params={"queue": "high_confidence", "limit": "20"})
    if high.status_code != 200:
        _fail(f"high queue {high.status_code}")
    forbidden = http.post(
        "/api/admin/duplicate-review/batch/preview",
        json={"action": "MERGE_CANDIDATE", "reason": "should fail", "pair_keys": ["22:28"]},
        headers=headers,
    )
    if forbidden.status_code != 400:
        _fail(f"batch merge candidate {forbidden.status_code} {forbidden.text}")
    specialist_pw = f"NsTest9{secrets.token_hex(8)}"
    spec = provision_staff_user(
        first_name="Dana",
        last_name="Iso",
        email=f"ds12.spec.{_stamp()}@northstar.example.test",
        password=specialist_pw,
        staff_role=REVOPS_SPECIALIST,
        client_ids=[_clients()["brown"]],
    )
    spec_csrf = _login(http, spec["user"]["email"], specialist_pw)
    denied = http.post("/api/admin/duplicate-review/analyze", json={}, headers={CSRF_HEADER: spec_csrf})
    if denied.status_code != 403 or denied.json().get("detail") != ADMIN_REQUIRED_DETAIL:
        _fail(f"specialist analyze {denied.status_code} {denied.json()}")
    denied_batch = http.post(
        "/api/admin/duplicate-review/batch/preview",
        json={"action": "ACCEPT_LIKELY_DUPLICATE", "reason": "nope", "pair_keys": ["22:28"]},
        headers={CSRF_HEADER: spec_csrf},
    )
    if denied_batch.status_code != 403:
        _fail(f"specialist batch {denied_batch.status_code}")
    with _enforcement_on():
        anon = TestClient(app)
        unauth = anon.post("/api/admin/duplicate-review/analyze", json={})
        if unauth.status_code != 401 or unauth.json().get("detail") != AUTH_REQUIRED_DETAIL:
            _fail(f"unauthenticated analyze {unauth.status_code} {unauth.json()}")
    if live_destructive_enabled() or refresh_meta().get("live_confirm_enabled"):
        _fail("destructive or LM confirm enabled")
    for path in (
        ROOT / "crm_import_plan.py",
        ROOT / "research_import_plan.py",
        ROOT / "leadmaster_refresh_http.py",
        ROOT / "leadmaster_refresh_plan.py",
    ):
        text = path.read_text(encoding="utf-8")
        if "company_duplicate_classifications" in text or "HIGH_CONFIDENCE_DUPLICATE" in text:
            _fail(f"{path.name} reads DS-12 classifications")
    source = (ROOT / "duplicate_classify.py").read_text(encoding="utf-8")
    if "company_merge_execute" in source or "create_merge_approval" in source:
        _fail("duplicate_classify imports merge execution")
    import test_add_company_duplicates

    rc = test_add_company_duplicates.main()
    if rc not in (0, None):
        _fail(f"add company duplicates rc={rc}")


def test_known_pairs_report() -> None:
    actor = _admin_user()
    with get_connection() as conn:
        analyze_duplicate_candidates(conn, actor=actor)
        stored = load_classifications(conn)
        results = {}
        for lo, hi, label in KNOWN_DUPES + KNOWN_MULTI + KNOWN_REVIEW:
            key = pair_key(lo, hi)
            row = stored.get(key) or classify_pair_readonly(conn, lo, hi)
            results[label] = {
                "pair": key,
                "classification": row.get("classification"),
                "confidence": row.get("confidence"),
                "survivor": row.get("proposed_survivor_company_id"),
            }
        edl = results["EDL"]
        if edl["classification"] not in {CLASS_HIGH, CLASS_LIKELY, CLASS_HUMAN}:
            _fail(f"EDL unexpected {edl}")
        green = results["Greenheck"]
        if green["classification"] == CLASS_HIGH:
            _fail("Greenheck classified HIGH_CONFIDENCE_DUPLICATE")
        print("DS12_KNOWN_RESULTS", results)


def test_ds_and_pilot_regressions() -> None:
    import test_data_steward_ds11

    test_data_steward_ds11.test_pair_ordering_schema_and_gates()
    test_data_steward_ds11.test_ds7_ds10_gate_regressions()


if __name__ == "__main__":
    tests = (
        test_production_path_untouched,
        test_pure_classifier_buckets,
        test_schema_analyze_idempotence_and_authority,
        test_http_admin_only_and_gates,
        test_known_pairs_report,
        test_ds_and_pilot_regressions,
    )
    for test in tests:
        test()
        print(f"{test.__name__}: ok")
    print("DS-12 isolated tests passed")
