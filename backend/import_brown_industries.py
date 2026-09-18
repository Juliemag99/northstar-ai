"""Additive import of Brown Industries as a second NorthStar client.

Does NOT modify or delete Carmeco data. Matches master companies conservatively.
Legacy Record No. is stored on client_company_relationships.
"""
from __future__ import annotations

import csv
import io
import json
import re
import shutil
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from db import DEFAULT_USER_EMAIL, DB_PATH as _CANONICAL_DB_PATH, get_connection

REPO_ROOT = Path(__file__).resolve().parent.parent
DATABASE_DIR = REPO_ROOT / "database"
DB_PATH = DATABASE_DIR / "northstar.db"
BACKUPS_DIR = DATABASE_DIR / "backups"
IMPORTS_DIR = DATABASE_DIR / "imports"

BROWN_DIR = Path(
    r"C:\Users\julie\OneDrive\OneDrive JRM\OneDrive\JRM Recruitment\NorthStar\Brown Industries"
)
MASTER_CSV = BROWN_DIR / "Brown industries master.csv"
NOTES_CSV = BROWN_DIR / "brown notes.csv"

CLIENT_CODE = "brown"
CLIENT_NAME = "Brown Industries"


@dataclass
class OverlapRow:
    brown_company: str
    brown_record_no: str
    carmeco_company: str
    carmeco_record_no: str
    match_reason: str
    match_confidence: str
    action_taken: str


@dataclass
class PossibleMatch:
    brown_company: str
    brown_record_no: str
    carmeco_company: str
    carmeco_record_no: str
    match_reason: str
    notes: str


@dataclass
class ImportReport:
    backup_path: str = ""
    brown_companies_imported: int = 0
    brown_contacts_imported: int = 0
    brown_companies_with_legacy_notes: int = 0
    brown_companies_without_legacy_notes: int = 0
    notes_skipped_outside_brown: int = 0
    existing_master_matched: int = 0
    new_master_created: int = 0
    overlaps: list[OverlapRow] = field(default_factory=list)
    possible_matches: list[PossibleMatch] = field(default_factory=list)
    whirlpool: dict = field(default_factory=dict)
    carmeco_integrity: dict = field(default_factory=dict)
    milestone_source_audit: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    cross_client_opportunities: dict = field(default_factory=dict)


def decode_csv_bytes(raw: bytes) -> tuple[str, str]:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return enc, raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return "latin-1", raw.decode("latin-1")


def load_csv(path: Path) -> tuple[str, list[dict[str, str | None]]]:
    enc, text = decode_csv_bytes(path.read_bytes())
    rows = list(csv.DictReader(io.StringIO(text)))
    return enc, rows


def cell(row: dict, *keys: str) -> str:
    for key in keys:
        if key in row and row[key] is not None:
            return str(row[key]).strip()
    return ""


def norm_name(value: str) -> str:
    s = (value or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    stop = {
        "inc",
        "llc",
        "ltd",
        "corp",
        "corporation",
        "co",
        "company",
        "the",
        "of",
    }
    return " ".join(t for t in s.split() if t not in stop)


def domain(value: str) -> str:
    w = (value or "").lower().strip()
    if not w or w in {"(blank)", "n/a", "na", "none", "-"}:
        return ""
    w = re.sub(r"^https?://", "", w)
    w = re.sub(r"^www\.", "", w)
    w = w.split("/")[0].strip(".")
    return w if w and "." in w else ""


def digits_phone(value: str) -> str:
    from contact_phone import split_phone_extension

    main, ext = split_phone_extension(value or "")
    source = main if ext else (value or "")
    d = re.sub(r"\D", "", source)
    return d[-10:] if len(d) >= 10 else d


def norm_addr(value: str) -> str:
    a = (value or "").lower()
    a = re.sub(r"[^a-z0-9]+", " ", a)
    drop = {
        "street",
        "st",
        "avenue",
        "ave",
        "road",
        "rd",
        "drive",
        "dr",
        "boulevard",
        "blvd",
        "lane",
        "ln",
        "suite",
        "ste",
        "north",
        "south",
        "east",
        "west",
        "n",
        "s",
        "e",
        "w",
    }
    return " ".join(t for t in a.split() if t not in drop)


def related_domains(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    # e.g. whirlpool.com / whirlpoolcorp.com
    a_root = a.split(".")[0]
    b_root = b.split(".")[0]
    return a_root.startswith(b_root) or b_root.startswith(a_root)


def backup_database() -> Path:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUPS_DIR / f"northstar_pre_brown_import_{stamp}.db"
    shutil.copy2(DB_PATH, dest)
    return dest


def company_payload_from_row(row: dict) -> dict[str, str]:
    primary_sic = cell(row, "Primary SIC Code") or cell(row, "SIC_CODE")
    primary_sic_desc = cell(row, "Primary SIC Code Description") or cell(
        row, "DESCRIPTION"
    )
    industry = (
        cell(row, "Type of Industry")
        or cell(row, "Industry")
        or cell(row, "INDUSTRY DESCRIPTION")
    )
    return {
        "company_name": cell(row, "Company"),
        "address": cell(row, "Address1", "Address"),
        "city": cell(row, "City"),
        "state": cell(row, "State"),
        "zip": cell(row, "Zip"),
        "website": cell(row, "Web Address"),
        "legacy_first_name": cell(row, "FirstName", "F Name"),
        "legacy_last_name": cell(row, "LastName", "L Name"),
        "legacy_title": cell(row, "Title"),
        "legacy_phone": cell(row, "Phone"),
        "legacy_alt_phone": cell(row, "Alt Phone"),
        "legacy_mobile": cell(row, "Mobile"),
        "legacy_email": cell(row, "Email"),
        "sales_volume_range": cell(row, "Location Sales Volume Range"),
        "location_sales_volume_range": cell(row, "Location Sales Volume Range"),
        "employee_size_range": cell(row, "Location Employee Size Range"),
        "primary_sic_code": primary_sic,
        "primary_sic_description": primary_sic_desc,
        "primary_naics_code": cell(row, "Primary NAICS Code"),
        "primary_naics_description": cell(row, "Primary NAICS Description"),
        "type_of_industry": industry,
        "customer_campaign": cell(row, "Customer Campaign"),
    }


def _score_hit(
    brown: dict,
    hit: sqlite3.Row,
    *,
    include_same_rn: bool = False,
    brown_rn: str = "",
) -> list[str]:
    b_name = cell(brown, "Company")
    b_norm = norm_name(b_name)
    b_dom = domain(cell(brown, "Web Address"))
    b_addr = norm_addr(cell(brown, "Address1"))
    b_city = cell(brown, "City").lower()
    b_state = cell(brown, "State").upper()
    b_phone = digits_phone(cell(brown, "Phone"))

    reasons: list[str] = []
    if include_same_rn and brown_rn and str(hit["external_record_no"]) == brown_rn:
        reasons.append("same_record_no")
    if norm_name(hit["company_name"]) == b_norm:
        reasons.append("normalized_name")
    c_addr = norm_addr(hit["address"])
    c_city = (hit["city"] or "").strip().lower()
    c_state = (hit["state"] or "").strip().upper()
    if b_addr and c_addr == b_addr and b_city == c_city and b_state == c_state:
        reasons.append("address_city_state")
    elif b_city and c_city == b_city and b_state and c_state == b_state:
        reasons.append("city_state")
    c_dom = domain(hit["website"])
    if b_dom and c_dom == b_dom:
        reasons.append("website_domain")
    elif related_domains(b_dom, c_dom):
        reasons.append("related_website_domain")
    if b_phone and digits_phone(hit["legacy_phone"]) == b_phone:
        reasons.append("phone")
    return reasons


def _is_high_confidence(reasons: list[str]) -> bool:
    if "same_record_no" in reasons and "normalized_name" in reasons:
        return True
    if (
        "normalized_name" in reasons
        and ("city_state" in reasons or "address_city_state" in reasons)
        and (
            "related_website_domain" in reasons
            or "website_domain" in reasons
            or "address_city_state" in reasons
            or "phone" in reasons
        )
    ):
        return True
    return False


def decide_match(
    brown_rn: str,
    brown: dict,
    carmeco_by_rn: dict[str, sqlite3.Row],
    carmeco_by_name: dict[str, list[sqlite3.Row]],
    carmeco_by_domain: dict[str, list[sqlite3.Row]],
) -> tuple[str, sqlite3.Row | None, str, list[PossibleMatch]]:
    """Return (confidence, matched_row|None, reason, possible_matches)."""
    possibles: list[PossibleMatch] = []
    b_name = cell(brown, "Company")
    b_norm = norm_name(b_name)
    b_dom = domain(cell(brown, "Web Address"))

    same_rn = carmeco_by_rn.get(brown_rn)
    name_hits = list(carmeco_by_name.get(b_norm, []))
    dom_hits = list(carmeco_by_domain.get(b_dom, [])) if b_dom else []

    candidates: dict[int, tuple[sqlite3.Row, list[str]]] = {}
    if same_rn is not None:
        candidates[int(same_rn["id"])] = (
            same_rn,
            _score_hit(brown, same_rn, include_same_rn=True, brown_rn=brown_rn),
        )
    for hit in name_hits + dom_hits:
        hid = int(hit["id"])
        if hid in candidates:
            continue
        candidates[hid] = (hit, _score_hit(brown, hit))

    best: tuple[sqlite3.Row, list[str]] | None = None
    for hit, reasons in candidates.values():
        if not _is_high_confidence(reasons):
            continue
        if best is None:
            best = (hit, reasons)
            continue
        # Prefer same Record No., then exact address match
        best_score = (
            ("same_record_no" in best[1]) * 10
            + ("address_city_state" in best[1]) * 5
            + len(best[1])
        )
        score = (
            ("same_record_no" in reasons) * 10
            + ("address_city_state" in reasons) * 5
            + len(reasons)
        )
        if score > best_score:
            best = (hit, reasons)

    chosen_id = int(best[0]["id"]) if best else None
    for hit, reasons in candidates.values():
        if chosen_id is not None and int(hit["id"]) == chosen_id:
            continue
        if "normalized_name" not in reasons and "website_domain" not in reasons:
            continue
        possibles.append(
            PossibleMatch(
                brown_company=b_name,
                brown_record_no=brown_rn,
                carmeco_company=str(hit["company_name"]),
                carmeco_record_no=str(hit["external_record_no"]),
                match_reason="+".join(reasons) if reasons else "weak_signal",
                notes="Not auto-merged; requires review.",
            )
        )

    if best is not None:
        return ("high", best[0], "+".join(best[1]), possibles)
    return ("none", None, "", possibles)


def ensure_brown_client(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO clients (code, name) VALUES (?, ?)",
        (CLIENT_CODE, CLIENT_NAME),
    )
    row = conn.execute(
        "SELECT id FROM clients WHERE code = ?", (CLIENT_CODE,)
    ).fetchone()
    assert row is not None
    return int(row["id"])


def wipe_brown_client_data(conn: sqlite3.Connection, brown_client_id: int) -> None:
    """Remove prior Brown import rows without touching Carmeco relationships/notes/contacts."""
    carmeco = conn.execute(
        "SELECT id FROM clients WHERE code = 'carmeco'"
    ).fetchone()
    carmeco_id = int(carmeco["id"]) if carmeco else None

    brown_company_ids = [
        int(r["company_id"])
        for r in conn.execute(
            "SELECT company_id FROM client_company_relationships WHERE client_id = ?",
            (brown_client_id,),
        )
    ]
    if not brown_company_ids:
        return

    conn.execute(
        "DELETE FROM legacy_notes WHERE client_id = ?", (brown_client_id,)
    )

    # Only delete contacts on Brown-only master companies (never shared with Carmeco)
    for company_id in brown_company_ids:
        shared = False
        if carmeco_id is not None:
            shared = (
                conn.execute(
                    """
                    SELECT 1 FROM client_company_relationships
                    WHERE client_id = ? AND company_id = ?
                    """,
                    (carmeco_id, company_id),
                ).fetchone()
                is not None
            )
        if shared:
            # Prior Brown contacts on a shared master: remove by Brown relationship RN only
            # when that RN differs from the Carmeco/master company RN (e.g. Whirlpool).
            brow_rn = conn.execute(
                """
                SELECT external_record_no FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                """,
                (brown_client_id, company_id),
            ).fetchone()
            master_rn = conn.execute(
                "SELECT external_record_no FROM companies WHERE id = ?",
                (company_id,),
            ).fetchone()
            if (
                brow_rn
                and master_rn
                and str(brow_rn["external_record_no"]).strip()
                != str(master_rn["external_record_no"]).strip()
            ):
                conn.execute(
                    """
                    DELETE FROM contacts
                    WHERE company_id = ? AND external_record_no = ?
                    """,
                    (company_id, str(brow_rn["external_record_no"]).strip()),
                )
            # Same RN shared companies: leave contacts alone on wipe (re-import would duplicate).
            # Abort re-import if shared-same-RN Brown contacts already exist.
            continue
        conn.execute("DELETE FROM contacts WHERE company_id = ?", (company_id,))

    for table in ("opportunity_dismissals", "opportunity_assignments"):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists:
            conn.execute(
                f"DELETE FROM {table} WHERE target_client_id = ?",
                (brown_client_id,),
            )

    # Guard: if any Brown CCR shares Record No. with Carmeco and Brown contacts exist,
    # require a clean DB rather than risk deleting Carmeco contacts.
    if carmeco_id is not None:
        conflict = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM client_company_relationships b
            JOIN client_company_relationships c
              ON c.company_id = b.company_id AND c.client_id = ?
            JOIN contacts ct
              ON ct.company_id = b.company_id
             AND TRIM(ct.external_record_no) = TRIM(b.external_record_no)
            WHERE b.client_id = ?
              AND TRIM(b.external_record_no) = (
                    SELECT TRIM(co.external_record_no) FROM companies co
                    WHERE co.id = b.company_id
              )
            """,
            (carmeco_id, brown_client_id),
        ).fetchone()["n"]
        if int(conflict) > 0:
            raise SystemExit(
                "Brown appears already imported onto shared Record Nos. "
                "Restore from backup before re-importing to avoid contact duplication."
            )

    conn.execute(
        "DELETE FROM client_company_relationships WHERE client_id = ?",
        (brown_client_id,),
    )

    for company_id in brown_company_ids:
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM client_company_relationships WHERE company_id = ?",
            (company_id,),
        ).fetchone()["n"]
        if int(remaining) == 0:
            conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))

def carmeco_counts(conn: sqlite3.Connection, *, max_contact_id: int | None = None) -> dict:
    carmeco = conn.execute(
        "SELECT id FROM clients WHERE code = 'carmeco'"
    ).fetchone()
    cid = int(carmeco["id"])
    companies = conn.execute(
        """
        SELECT COUNT(*) AS n FROM client_company_relationships WHERE client_id = ?
        """,
        (cid,),
    ).fetchone()["n"]
    if max_contact_id is None:
        contacts = conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]
    else:
        # Pre-Brown contact rows only (shared Record Nos must not inflate Carmeco)
        contacts = conn.execute(
            "SELECT COUNT(*) AS n FROM contacts WHERE id <= ?",
            (max_contact_id,),
        ).fetchone()["n"]
    notes = conn.execute(
        "SELECT COUNT(DISTINCT company_id) AS n FROM legacy_notes WHERE client_id = ?",
        (cid,),
    ).fetchone()["n"]
    return {
        "client_relationships": int(companies),
        "contacts": int(contacts),
        "companies_with_legacy_notes": int(notes),
        "max_contact_id": max_contact_id,
    }


def run_import() -> ImportReport:
    report = ImportReport()
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")
    if not MASTER_CSV.exists() or not NOTES_CSV.exists():
        raise SystemExit("Brown source CSVs not found.")

    report.backup_path = str(backup_database())
    print(f"Backup: {report.backup_path}")

    from db import ensure_schema

    ensure_schema(DB_PATH)

    _, master_rows = load_csv(MASTER_CSV)
    _, notes_rows = load_csv(NOTES_CSV)

    by_rec: dict[str, list[dict]] = defaultdict(list)
    for i, row in enumerate(master_rows):
        rn = cell(row, "Record No.")
        if not rn:
            continue
        row = dict(row)
        row["_source_row_index"] = i + 1  # 1-based data row
        by_rec[rn].append(row)

    brown_companies = {rn: rows[0] for rn, rows in by_rec.items()}
    if len(brown_companies) != 100:
        print(f"WARNING: expected 100 Brown companies, found {len(brown_companies)}")

    notes_by: dict[str, list[str]] = defaultdict(list)
    for row in notes_rows:
        rn = cell(row, "Record No.")
        txt = cell(row, "Sales Rep Comments/Notes")
        if rn:
            notes_by[rn].append(txt)

    brown_rns = set(brown_companies)
    notes_outside = [rn for rn in notes_by if rn not in brown_rns]
    report.notes_skipped_outside_brown = len(notes_outside)

    # Milestone source audit — Brown master has no dedicated status/milestone columns
    report.milestone_source_audit = {
        "status_column": None,
        "appointment_set_field": None,
        "quote_field": None,
        "purchase_order_field": None,
        "weblead_field": None,
        "hot_field": None,
        "customer_campaign_present": True,
        "decision": (
            "Do NOT infer Appointment Set / Quote / Purchase Order / WebLead / Hot "
            "from Customer Campaign tokens (e.g. 'Brown Web', '... Hot'). "
            "No dedicated Brown status or milestone columns exist in the source. "
            "Brown CCR status left blank; no revenue_milestones created from import."
        ),
    }

    if Path(DB_PATH).resolve() != Path(_CANONICAL_DB_PATH).resolve():
        raise RuntimeError(
            f"import_brown_industries DB_PATH ({DB_PATH}) diverges from db.DB_PATH "
            f"({_CANONICAL_DB_PATH}); refusing raw connect."
        )
    # Canonical helper registers CRM identity UDFs required by identity-key triggers.
    conn = get_connection()

    max_contact_before = int(
        conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM contacts").fetchone()["n"]
    )
    before_carmeco = carmeco_counts(conn, max_contact_id=max_contact_before)
    brown_client_id = ensure_brown_client(conn)
    wipe_brown_client_data(conn, brown_client_id)
    # Refresh contact ceiling after any prior Brown wipe
    max_contact_before = int(
        conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM contacts").fetchone()["n"]
    )
    before_carmeco = carmeco_counts(conn, max_contact_id=max_contact_before)

    # Assign Julie to Brown
    julie = conn.execute(
        "SELECT id FROM users WHERE lower(email) = lower(?)",
        (DEFAULT_USER_EMAIL,),
    ).fetchone()
    if julie:
        conn.execute(
            """
            INSERT OR IGNORE INTO user_client_assignments (user_id, client_id, role, active)
            VALUES (?, ?, 'account_executive', 1)
            """,
            (int(julie["id"]), brown_client_id),
        )

    carmeco_client = conn.execute(
        "SELECT id FROM clients WHERE code = 'carmeco'"
    ).fetchone()
    carmeco_client_id = int(carmeco_client["id"])

    carmeco_rows = list(
        conn.execute(
            """
            SELECT co.id, co.external_record_no, co.company_name, co.address,
                   co.city, co.state, co.zip, co.website, co.legacy_phone
            FROM companies co
            JOIN client_company_relationships ccr
              ON ccr.company_id = co.id AND ccr.client_id = ?
            """,
            (carmeco_client_id,),
        )
    )
    carmeco_by_rn = {str(r["external_record_no"]): r for r in carmeco_rows}
    carmeco_by_name: dict[str, list[sqlite3.Row]] = defaultdict(list)
    carmeco_by_domain: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in carmeco_rows:
        carmeco_by_name[norm_name(r["company_name"])].append(r)
        d = domain(r["website"])
        if d:
            carmeco_by_domain[d].append(r)

    # Map brown RN -> company_id
    brown_company_ids: dict[str, int] = {}
    matched_possible_seen: set[tuple[str, str]] = set()

    for rn, brow in sorted(brown_companies.items(), key=lambda x: x[0]):
        confidence, match, reason, possibles = decide_match(
            rn, brow, carmeco_by_rn, carmeco_by_name, carmeco_by_domain
        )
        for p in possibles:
            key = (p.brown_record_no, p.carmeco_record_no)
            # Skip possibles that are the high-confidence match we already take
            if match is not None and p.carmeco_record_no == str(match["external_record_no"]):
                continue
            if key not in matched_possible_seen:
                matched_possible_seen.add(key)
                report.possible_matches.append(p)

        payload = company_payload_from_row(brow)

        if confidence == "high" and match is not None:
            company_id = int(match["id"])
            report.existing_master_matched += 1
            report.overlaps.append(
                OverlapRow(
                    brown_company=payload["company_name"],
                    brown_record_no=rn,
                    carmeco_company=str(match["company_name"]),
                    carmeco_record_no=str(match["external_record_no"]),
                    match_reason=reason,
                    match_confidence="high",
                    action_taken="linked_brown_relationship_to_existing_master",
                )
            )
            # Do not overwrite master company profile (preserve Carmeco source fields)
        else:
            # New master company — Brown Record No. becomes master external_record_no
            # only when unused.
            existing = conn.execute(
                "SELECT id, company_name FROM companies WHERE external_record_no = ?",
                (rn,),
            ).fetchone()
            if existing is not None:
                # Collision without high-confidence identity match — should not happen
                # after decide_match; treat as possible match and create synthetic RN.
                synth = f"brown:{rn}"
                print(
                    f"WARNING: Record No. {rn} exists as '{existing['company_name']}' "
                    f"without high-confidence match; creating master as {synth}"
                )
                company_id = _insert_company(conn, synth, payload)
                report.possible_matches.append(
                    PossibleMatch(
                        brown_company=payload["company_name"],
                        brown_record_no=rn,
                        carmeco_company=str(existing["company_name"]),
                        carmeco_record_no=rn,
                        match_reason="same_record_no_without_identity_corroboration",
                        notes=f"Created separate master under {synth}; review required.",
                    )
                )
            else:
                company_id = _insert_company(conn, rn, payload)
            report.new_master_created += 1

        # Brown client-company relationship (Record No. on relationship)
        conn.execute(
            """
            INSERT INTO client_company_relationships (
                client_id, company_id, external_record_no, status, assigned_user_id,
                is_hot, created_at, updated_at
            ) VALUES (?, ?, ?, '', ?, 0, datetime('now'), datetime('now'))
            ON CONFLICT(client_id, company_id) DO UPDATE SET
                external_record_no = excluded.external_record_no,
                updated_at = datetime('now')
            """,
            (
                brown_client_id,
                company_id,
                rn,
                int(julie["id"]) if julie else None,
            ),
        )
        brown_company_ids[rn] = company_id

        if rn == "1002756":
            report.whirlpool = {
                "brown_record_no": rn,
                "brown_address": f"{payload['address']}, {payload['city']} {payload['state']} {payload['zip']}",
                "brown_website": payload["website"],
                "action": "linked_to_existing_master"
                if confidence == "high"
                else "created_new_master",
                "match_reason": reason,
                "match_confidence": confidence,
                "master_company_id": company_id,
                "carmeco_record_no": str(match["external_record_no"])
                if match is not None
                else None,
                "carmeco_company": str(match["company_name"]) if match is not None else None,
                "carmeco_address": (
                    f"{match['address']}, {match['city']} {match['state']} {match['zip']}"
                    if match is not None
                    else None
                ),
                "notes": (
                    "Same legal name, same city/state/ZIP (Benton Harbor MI 49022), "
                    "related corporate domains (whirlpoolcorp.com / whirlpool.com). "
                    "Street addresses differ (corporate campus locations). "
                    "ONE master company; TWO client relationships. Notes kept separate."
                ),
            }

    # Contacts — all 624 rows; do not destructively merge with Carmeco contacts
    contact_count = 0
    for rn, rows in by_rec.items():
        company_id = brown_company_ids[rn]
        for row in rows:
            conn.execute(
                """
                INSERT INTO contacts (
                    company_id, external_record_no,
                    first_name, last_name, title, phone, alt_phone, email,
                    source_row_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company_id,
                    rn,
                    cell(row, "FirstName"),
                    cell(row, "LastName"),
                    cell(row, "Title"),
                    cell(row, "Phone"),
                    cell(row, "Alt Phone"),
                    cell(row, "Email"),
                    int(row["_source_row_index"]),
                ),
            )
            contact_count += 1
    report.brown_contacts_imported = contact_count
    report.brown_companies_imported = len(brown_company_ids)

    # Legacy notes — only Brown Record Nos; never borrow
    with_notes = 0
    without_notes = 0
    for rn, company_id in brown_company_ids.items():
        texts = [t for t in notes_by.get(rn, []) if t.strip()]
        if not texts:
            without_notes += 1
            continue
        # Prefer first non-blank note (notes file should be 1:1 per Record No.)
        note_text = texts[0]
        conn.execute(
            """
            INSERT INTO legacy_notes (client_id, company_id, note_text, source_field)
            VALUES (?, ?, ?, ?)
            """,
            (brown_client_id, company_id, note_text, "Sales Rep Comments/Notes"),
        )
        with_notes += 1
    report.brown_companies_with_legacy_notes = with_notes
    report.brown_companies_without_legacy_notes = without_notes

    conn.commit()

    after_carmeco = carmeco_counts(conn, max_contact_id=max_contact_before)
    still_present = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM contacts WHERE id <= ?",
            (max_contact_before,),
        ).fetchone()["n"]
    )
    report.carmeco_integrity = {
        "before": before_carmeco,
        "after": after_carmeco,
        "pre_brown_contact_rows_still_present": still_present
        == before_carmeco["contacts"],
        "carmeco_relationships_unchanged": before_carmeco["client_relationships"]
        == after_carmeco["client_relationships"]
        == 402,
        "carmeco_notes_unchanged": before_carmeco["companies_with_legacy_notes"]
        == after_carmeco["companies_with_legacy_notes"]
        == 382,
        "carmeco_contacts_unchanged": before_carmeco["contacts"]
        == after_carmeco["contacts"]
        == 2000,
        "total_contacts_after": int(
            conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]
        ),
    }

    # Validate all 100 Brown Record Nos
    validation_errors: list[str] = []
    for rn, brow in brown_companies.items():
        ccr = conn.execute(
            """
            SELECT ccr.*, co.company_name, co.id AS company_id
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            WHERE ccr.client_id = ? AND ccr.external_record_no = ?
            """,
            (brown_client_id, rn),
        ).fetchone()
        if ccr is None:
            validation_errors.append(f"{rn}: missing Brown relationship")
            continue
        expected_name = cell(brow, "Company")
        if norm_name(ccr["company_name"]) != norm_name(expected_name) and rn != "1002756":
            # Whirlpool master keeps Carmeco name (same); others should match
            if norm_name(ccr["company_name"]) != norm_name(expected_name):
                validation_errors.append(
                    f"{rn}: company name mismatch DB={ccr['company_name']!r} src={expected_name!r}"
                )
        n_contacts = conn.execute(
            """
            SELECT COUNT(*) AS n FROM contacts
            WHERE company_id = ? AND external_record_no = ?
            """,
            (int(ccr["company_id"]), rn),
        ).fetchone()["n"]
        expected_contacts = len(by_rec[rn])
        if int(n_contacts) != expected_contacts:
            validation_errors.append(
                f"{rn}: contacts {n_contacts} != expected {expected_contacts}"
            )
        note = conn.execute(
            """
            SELECT COUNT(*) AS n FROM legacy_notes
            WHERE client_id = ? AND company_id = ?
            """,
            (brown_client_id, int(ccr["company_id"])),
        ).fetchone()["n"]
        should_have = 1 if any(t.strip() for t in notes_by.get(rn, [])) else 0
        if int(note) != should_have:
            validation_errors.append(
                f"{rn}: legacy notes {note} != expected {should_have}"
            )

    report.validation = {
        "brown_record_nos_checked": len(brown_companies),
        "errors": validation_errors,
        "ok": len(validation_errors) == 0,
        "no_notes_record_nos": sorted(
            rn
            for rn in brown_rns
            if not any(t.strip() for t in notes_by.get(rn, []))
        ),
    }

    conn.close()

    # Rebuild FTS + seed assignments
    from db import ensure_schema as _ensure
    from search_data import rebuild_search_index

    _ensure(DB_PATH)
    rebuild_search_index()

    # Cross-client opportunities
    report.cross_client_opportunities = _run_opportunity_report()

    IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = IMPORTS_DIR / "brown_import_report.json"
    out.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
    print(f"Report written: {out}")
    return report


def _insert_company(conn: sqlite3.Connection, record_no: str, payload: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO companies (
            external_record_no, company_name, address, city, state, zip, website,
            legacy_first_name, legacy_last_name, legacy_title,
            legacy_phone, legacy_alt_phone, legacy_mobile, legacy_email,
            sales_volume_range, location_sales_volume_range, employee_size_range,
            primary_sic_code, primary_sic_description,
            primary_naics_code, primary_naics_description,
            type_of_industry, customer_campaign
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?,
            ?, ?,
            ?, ?
        )
        """,
        (
            record_no,
            payload["company_name"],
            payload["address"],
            payload["city"],
            payload["state"],
            payload["zip"],
            payload["website"],
            payload["legacy_first_name"],
            payload["legacy_last_name"],
            payload["legacy_title"],
            payload["legacy_phone"],
            payload["legacy_alt_phone"],
            payload["legacy_mobile"],
            payload["legacy_email"],
            payload["sales_volume_range"],
            payload["location_sales_volume_range"],
            payload["employee_size_range"],
            payload["primary_sic_code"],
            payload["primary_sic_description"],
            payload["primary_naics_code"],
            payload["primary_naics_description"],
            payload["type_of_industry"],
            payload["customer_campaign"],
        ),
    )
    return int(cur.lastrowid)


def _run_opportunity_report() -> dict:
    try:
        from access import get_default_user
        from opportunities_data import list_cross_client_opportunities
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

    user = get_default_user()
    if user is None:
        return {"error": "No default user"}

    result: dict = {}
    with get_connection() as conn:
        clients = {
            str(r["code"]): int(r["id"])
            for r in conn.execute("SELECT id, code, name FROM clients")
        }

    for code, label in (("carmeco", "Carmeco"), ("brown", "Brown Industries")):
        cid = clients.get(code)
        if cid is None:
            result[label] = {"error": "client missing"}
            continue
        opp = list_cross_client_opportunities(
            user_id=user.id, target_client_id=cid
        )
        items = [
            {
                "company_name": o.company_name,
                "external_record_no": o.external_record_no,
                "opportunity_score": o.opportunity_score,
                "score_label": o.score_label,
                "target_client_activity": o.target_client_activity,
                "strongest_signal": o.strongest_signal,
                "has_target_relationship": o.has_target_relationship,
                "signals": [
                    {
                        "client_name": s.client_name,
                        "milestone_type": s.milestone_type,
                        "milestone_date": s.milestone_date,
                    }
                    for s in (o.signal_history or [])
                ],
            }
            for o in (opp.opportunities or [])
        ]
        result[label] = {
            "target_client_id": cid,
            "count": len(items),
            "data_note": getattr(opp, "data_note", "") or "",
            "top": items[:25],
        }
    return result


def main() -> None:
    report = run_import()
    print("\n=== IMPORT RESULTS ===")
    print(f"Brown companies: {report.brown_companies_imported}")
    print(f"Brown contacts: {report.brown_contacts_imported}")
    print(f"With notes: {report.brown_companies_with_legacy_notes}")
    print(f"Without notes: {report.brown_companies_without_legacy_notes}")
    print(f"Notes skipped (outside Brown set): {report.notes_skipped_outside_brown}")
    print(f"Matched existing masters: {report.existing_master_matched}")
    print(f"New masters created: {report.new_master_created}")
    print(f"Possible matches: {len(report.possible_matches)}")
    print(f"Validation OK: {report.validation.get('ok')}")
    if report.validation.get("errors"):
        for e in report.validation["errors"]:
            print("  ERR", e)
    print("\nWhirlpool:", json.dumps(report.whirlpool, indent=2))
    print("\nCarmeco integrity:", json.dumps(report.carmeco_integrity, indent=2))
    print(
        "\nCross-client:",
        json.dumps(report.cross_client_opportunities, indent=2)[:2000],
    )


if __name__ == "__main__":
    main()
