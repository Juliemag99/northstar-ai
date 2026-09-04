"""Research Company orchestration (Phase 2A).

Flow:
1. Resolve company + required Working For client (never guess).
2. Gather WHAT NORTHSTAR ALREADY KNOWS (authorized only).
3. Run research providers (Public Web active; ZoomInfo stub).
4. Compare vs master → verified matches / proposed updates (Pending Review).
5. Store shared company intelligence on the master company.
6. Compute FIT FOR [CLIENT] on the client-company relationship.
7. Approve/Reject writebacks are explicit human actions only.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from access import get_default_user, resolve_visibility_client_ids, user_can_access_client
from db import get_connection
from models import (
    ResearchApproveRequest,
    ResearchCompanyResponse,
    ResearchDecisionSummary,
    ResearchEngagementAssessment,
    ResearchFindingView,
    ResearchFitView,
    ResearchNorthStarKnown,
    ResearchProposedUpdateView,
    ResearchRejectRequest,
    ResearchStartRequest,
    ResearchVerificationItem,
)
from opportunities_data import list_cross_client_opportunities
from research_providers import (
    NOT_VERIFIED,
    ResearchContext,
    get_research_providers,
    normalize_website,
)

# Master company fields that may receive approved research writebacks.
APPROVABLE_MASTER_FIELDS = {
    "website": "website",
    "company_name": "company_name",
    "address": "address",
    "city": "city",
    "state": "state",
    "zip": "zip",
    "type_of_industry": "type_of_industry",
}

DEFAULT_CLIENT_PROFILES: dict[str, dict[str, str]] = {
    # Carmeco — approved strategy-session profile (do not invent beyond this).
    "carmeco": {
        "summary": (
            "Primary service: metal stamping. Secondary: laser cutting, welding, "
            "fabrication, 3-axis machining, wet paint, and logistics. Serves OEMs and "
            "manufacturers needing recurring production metal components."
        ),
        "primary_service": "Metal stamping",
        "secondary_services": (
            "Laser cutting; Welding; Fabrication; 3-axis machining; Wet paint; Logistics"
        ),
        "ideal_customer_types": (
            "OEMs and manufacturers requiring recurring production metal components."
        ),
        "manufacturing_processes_sought": (
            "Primary: Outsourced metal stamping. "
            "Secondary: Laser cutting; Welding; Fabrication."
        ),
        "production_preference": (
            "Low through high-volume production is acceptable. "
            "Strong preference for ongoing/recurring production. "
            "New projects/programs are attractive entry points. "
            "Existing production parts are attractive. "
            "Overflow/capacity problems are attractive. "
            "Companies considering moving stamping out-of-house are attractive."
        ),
        "stamping_capability": (
            "20 tons through 1,200 tons; "
            "Bed size up to approximately 63 x 157 inches; "
            "Material thickness approximately .013 through .5 inches; "
            "Coil capacity up to approximately 30,000 lb / 60 inches wide."
        ),
        "tooling_notes": (
            "Existing tooling is a positive signal. "
            "Lack of tooling is NOT an automatic disqualifier. "
            "Carmeco has nearby tooling relationships for repair/build support."
        ),
        "target_industries": (
            "Industrial equipment / machinery; Lawn equipment; Garage doors; "
            "Transportation; Trucking / suspension / semi; Tier 2 automotive; "
            "Energy & power; Tech / electronics; HVAC; Appliance"
        ),
        "target_capabilities": (
            "metal stamping (primary); laser cutting; welding; fabrication; "
            "3-axis machining; wet paint; logistics"
        ),
        "target_products": (
            "recurring production metal components; stamped components"
        ),
        "geographic_preferences": (
            "Approximately 600-mile radius from Lebanon, Missouri. "
            "Soft preference only — not an absolute exclusion unless later marked mandatory."
        ),
        "company_size_preferences": "",
        "positive_fit_signals": (
            "Recurring production requirement; Existing tooling; New project/program; "
            "Growth; Production overflow; Capacity constraints; Existing stamped components; "
            "Manufacturer currently stamping internally but potentially willing to outsource; "
            "Multiple divisions/locations that could provide additional opportunities; "
            "Previous successful commercial history"
        ),
        "negative_fit_signals": (
            "One-off work only; No recurring/frequent production requirement; "
            "No identifiable need for stamped or related production metal components"
        ),
        "fit_weighting_notes": (
            "STAMPING is Carmeco's primary focus. Possible Fit requires a meaningful "
            "stamping or stamped/formed-component signal — generic manufacturing, "
            "cutting, fabrication, welding, industry match, or product type alone is "
            "Insufficient Information, not Possible Fit. Strong Fit requires meaningful "
            "evidence of a realistic recurring/outsourced stamping opportunity. "
            "Cross-client or own-client commercial history may increase confidence but "
            "must not independently determine Possible Fit or Strong Fit. Missing "
            "information is not the same as negative evidence (Weak Fit)."
        ),
        "notes": (
            "Approved strategy-session profile. Company size: no preference currently "
            "established — do not invent one."
        ),
    },
    # Brown — thin seed only; do not expand until separately approved.
    "brown": {
        "summary": (
            "Industrial / manufacturing sales focus seeking plants and OEMs with "
            "metal fabrication, stamping, and related production needs."
        ),
        "target_industries": "industrial manufacturing, OEM, appliance, agriculture, trailer",
        "target_capabilities": "metal fabrication, stamping, welding, machining, assembly",
        "target_products": "manufactured components and production partnerships",
        "notes": "Cross-client Carmeco history can inform Brown opportunity without changing Brown status.",
        "primary_service": "",
        "secondary_services": "",
        "ideal_customer_types": "",
        "manufacturing_processes_sought": "",
        "production_preference": "",
        "stamping_capability": "",
        "tooling_notes": "",
        "geographic_preferences": "",
        "company_size_preferences": "",
        "positive_fit_signals": "",
        "negative_fit_signals": "",
        "fit_weighting_notes": "",
    },
}

PROFILE_EXTENDED_COLUMNS = (
    "primary_service",
    "secondary_services",
    "ideal_customer_types",
    "manufacturing_processes_sought",
    "production_preference",
    "stamping_capability",
    "tooling_notes",
    "geographic_preferences",
    "company_size_preferences",
    "positive_fit_signals",
    "negative_fit_signals",
    "fit_weighting_notes",
)

# Fields required before Strong Fit is allowed (campaign TARGET PROFILE only).
# Administrative Client Information (phone, owner, location, website) must NOT
# appear here — incomplete client admin data does not block Strong Fit.
PROFILE_STRONG_FIT_REQUIRED = (
    "primary_service",
    "ideal_customer_types",
    "manufacturing_processes_sought",
    "target_industries",
    "positive_fit_signals",
    "negative_fit_signals",
    "fit_weighting_notes",
)

# Explicit fit rating when the selected campaign has no usable ICP criteria.
FIT_CRITERIA_NOT_CONFIGURED = "Campaign Criteria Not Configured"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank(value: object) -> str:
    return str(value or "").strip()


_CRM_MATCH_LABELS = {
    "existing": "Already in NorthStar",
    "possible_match": "Possible Match",
    "new": "New Contact",
}


def _load_prospecting_guidance(client_id: int) -> str:
    try:
        from client_knowledge_data import get_stored_knowledge_field

        return _blank(
            get_stored_knowledge_field(client_id, "strategy", "prospecting_guidance")
        )
    except Exception:
        return ""


def _resolve_research_personas(
    conn,
    *,
    working_id: int,
    campaign_id: int | None,
) -> tuple[list[str], str, dict | None]:
    from client_setup_data import get_campaign_for_client
    from research_people import resolve_target_personas

    campaign = get_campaign_for_client(conn, working_id, campaign_id)
    target_titles = _blank((campaign or {}).get("target_titles"))
    guidance = _load_prospecting_guidance(working_id)
    personas, source = resolve_target_personas(
        target_titles=target_titles,
        prospecting_guidance=guidance,
    )
    return personas, source, campaign


def _enrich_public_contact(
    conn,
    company_id: int,
    finding: ResearchFindingView,
    *,
    personas: list[str] | None = None,
) -> ResearchFindingView:
    from crm_add_data import match_contact
    from models import CrmAddContactInput
    from research_people import (
        classify_persona_relevance,
        pack_contact_meta,
        unpack_contact_meta_full,
    )

    meta = unpack_contact_meta_full(finding.field_key)
    finding.linkedin_url = _blank(meta.get("linkedin_url"))
    finding.linkedin_derived = bool(meta.get("linkedin_derived"))
    labels: list[str] = []
    for s in meta.get("sources") or []:
        if not isinstance(s, dict):
            continue
        name = _blank(s.get("name"))
        if "linkedin" in name.lower():
            name = "LinkedIn"
        if name and name not in labels:
            labels.append(name)
    if finding.linkedin_url and "LinkedIn" not in labels:
        labels.insert(0, "LinkedIn")
    if not labels and finding.source_name:
        for part in re.split(r"\s*\+\s*", finding.source_name):
            p = _blank(part)
            if p and p not in labels:
                labels.append(p)
    finding.source_labels = labels[:6]
    if not finding.source_url and finding.linkedin_url:
        finding.source_url = finding.linkedin_url

    # Authoritative relevance from campaign personas (when available)
    persona_list = list(personas or [])
    if persona_list and finding.contact_title:
        tier, persona, why = classify_persona_relevance(
            finding.contact_title, persona_list
        )
        finding.relevance_tier = tier
        finding.matched_persona = persona
        finding.why_relevant = why
        finding.field_key = pack_contact_meta(
            persona,
            why,
            linkedin_url=finding.linkedin_url,
            sources=list(meta.get("sources") or []),
            company=_blank(meta.get("company")),
            linkedin_derived=finding.linkedin_derived,
            relevance_tier=tier,
        )
    else:
        finding.matched_persona = _blank(meta.get("persona"))
        finding.why_relevant = _blank(meta.get("why"))
        finding.relevance_tier = _blank(meta.get("relevance_tier")) or (
            "HIGH"
            if finding.matched_persona
            else "NOT_TARGET"
        )

    match = match_contact(
        conn,
        company_id,
        CrmAddContactInput(
            full_name=_blank(finding.contact_name),
            title=_blank(finding.contact_title),
            email=_blank(finding.contact_email),
            phone=_blank(finding.contact_phone),
            linkedin=_blank(finding.linkedin_url),
        ),
    )
    status = _blank(match.get("status")) or "new"
    finding.crm_match_status = status
    finding.crm_match_label = _CRM_MATCH_LABELS.get(status, "New Contact")
    finding.crm_match_reasons = list(match.get("reasons") or [])
    if status in {"existing", "possible_match"}:
        finding.crm_matched_contact_name = _blank(match.get("matched_name"))
        cid = match.get("contact_id")
        finding.crm_matched_contact_id = int(cid) if cid else None
    else:
        finding.crm_matched_contact_name = ""
        finding.crm_matched_contact_id = None
    return finding


def _extract_people_discovery(finding_rows: list[ResearchFindingView]) -> dict:
    for f in finding_rows:
        if f.finding_type == "people_discovery":
            try:
                data = json.loads(f.value or "{}")
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
    return {}


def _public_contact_views(
    conn, company_id: int, finding_rows: list[ResearchFindingView]
) -> list[ResearchFindingView]:
    from research_people import RELEVANCE_RANK, pack_contact_meta, unpack_contact_meta_full

    pd = _extract_people_discovery(finding_rows)
    personas = [
        _blank(p) for p in (pd.get("personas") or []) if _blank(p)
    ]

    enriched: list[ResearchFindingView] = []
    for f in finding_rows:
        if not f.is_public_contact or f.finding_type == "people_discovery":
            continue
        enriched.append(
            _enrich_public_contact(conn, company_id, f, personas=personas)
        )

    # Cross-source collapse by person name (research findings may arrive from multiple passes)
    by_name: dict[str, ResearchFindingView] = {}
    for f in enriched:
        key = re.sub(r"[^a-z\s]", "", _blank(f.contact_name).lower())
        key = " ".join(key.split())
        if not key:
            continue
        if key not in by_name:
            by_name[key] = f
            continue
        existing = by_name[key]
        prefer = (
            RELEVANCE_RANK.get(f.relevance_tier, 0),
            {"high": 3, "medium": 2, "low": 1}.get(f.confidence, 0),
            1 if f.linkedin_url else 0,
        ) > (
            RELEVANCE_RANK.get(existing.relevance_tier, 0),
            {"high": 3, "medium": 2, "low": 1}.get(existing.confidence, 0),
            1 if existing.linkedin_url else 0,
        )
        primary = f if prefer else existing
        secondary = existing if prefer else f
        labels = list(primary.source_labels or [])
        for lab in secondary.source_labels or []:
            if lab not in labels:
                labels.append(lab)
        primary.source_labels = labels[:6]
        if not primary.linkedin_url and secondary.linkedin_url:
            primary.linkedin_url = secondary.linkedin_url
        if primary.linkedin_url and "LinkedIn" not in primary.source_labels:
            primary.source_labels = ["LinkedIn", *primary.source_labels][:6]
        if RELEVANCE_RANK.get(secondary.relevance_tier, 0) > RELEVANCE_RANK.get(
            primary.relevance_tier, 0
        ):
            primary.relevance_tier = secondary.relevance_tier
            primary.why_relevant = secondary.why_relevant
            primary.matched_persona = secondary.matched_persona
        try:
            pm = unpack_contact_meta_full(primary.field_key)
            sm = unpack_contact_meta_full(secondary.field_key)
            sources = list(pm.get("sources") or [])
            for s in sm.get("sources") or []:
                if s not in sources:
                    sources.append(s)
            primary.field_key = pack_contact_meta(
                primary.matched_persona or pm.get("persona", ""),
                primary.why_relevant or pm.get("why", ""),
                linkedin_url=primary.linkedin_url
                or _blank(pm.get("linkedin_url"))
                or _blank(sm.get("linkedin_url")),
                sources=sources[:8],
                company=_blank(pm.get("company")) or _blank(sm.get("company")),
                linkedin_derived=bool(
                    pm.get("linkedin_derived") or sm.get("linkedin_derived")
                ),
                relevance_tier=primary.relevance_tier
                or _blank(pm.get("relevance_tier"))
                or _blank(sm.get("relevance_tier")),
            )
        except Exception:
            pass
        by_name[key] = primary

    out = list(by_name.values())
    conf_rank = {"high": 3, "medium": 2, "low": 1}
    # Persona relevance first; CRM match status must not override
    out.sort(
        key=lambda x: (
            RELEVANCE_RANK.get(x.relevance_tier or "NOT_TARGET", 0),
            conf_rank.get(x.confidence, 0),
            1 if x.linkedin_url else 0,
        ),
        reverse=True,
    )
    return out


def _placeholders(ids: list[int]) -> str:
    return ",".join("?" for _ in ids)


def ensure_research_schema(conn=None) -> None:
    """Idempotent research tables + seed/update client target profiles."""
    owns = conn is None
    if owns:
        conn = get_connection()
    try:
        # Additive columns for deeper Phase 2A evidence
        finding_cols = {
            str(r["name"])
            for r in conn.execute("PRAGMA table_info(company_research_findings)").fetchall()
        }
        if finding_cols:
            if "evidence_level" not in finding_cols:
                conn.execute(
                    "ALTER TABLE company_research_findings "
                    "ADD COLUMN evidence_level TEXT NOT NULL DEFAULT 'verified'"
                )
            if "page_title" not in finding_cols:
                conn.execute(
                    "ALTER TABLE company_research_findings "
                    "ADD COLUMN page_title TEXT NOT NULL DEFAULT ''"
                )

        # Extended ICP columns on client_target_profiles
        profile_cols = {
            str(r["name"])
            for r in conn.execute("PRAGMA table_info(client_target_profiles)").fetchall()
        }
        if profile_cols:
            for col in PROFILE_EXTENDED_COLUMNS:
                if col not in profile_cols:
                    conn.execute(
                        f"ALTER TABLE client_target_profiles "
                        f"ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                    )

        _seed_client_target_profiles(conn)
        _apply_approved_carmeco_profile(conn)
        if owns:
            conn.commit()
    finally:
        if owns:
            conn.close()


def _seed_client_target_profiles(conn) -> None:
    """Insert missing profiles only — never overwrite Brown or existing Carmeco here."""
    rows = conn.execute("SELECT id, code, name FROM clients").fetchall()
    for r in rows:
        cid = int(r["id"])
        code = _blank(r["code"]).lower()
        name = _blank(r["name"]).lower()
        profile = None
        if "carmeco" in code or "carmeco" in name:
            profile = DEFAULT_CLIENT_PROFILES["carmeco"]
        elif "brown" in code or "brown" in name:
            profile = DEFAULT_CLIENT_PROFILES["brown"]
        if not profile:
            continue
        existing = conn.execute(
            "SELECT client_id FROM client_target_profiles WHERE client_id = ?",
            (cid,),
        ).fetchone()
        if existing:
            continue
        conn.execute(
            f"""
            INSERT INTO client_target_profiles (
                client_id, summary, target_industries, target_capabilities,
                target_products, notes, {", ".join(PROFILE_EXTENDED_COLUMNS)}, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, {", ".join("?" for _ in PROFILE_EXTENDED_COLUMNS)}, ?)
            """,
            (
                cid,
                profile.get("summary", ""),
                profile.get("target_industries", ""),
                profile.get("target_capabilities", ""),
                profile.get("target_products", ""),
                profile.get("notes", ""),
                *[profile.get(c, "") for c in PROFILE_EXTENDED_COLUMNS],
                _now(),
            ),
        )


def _apply_approved_carmeco_profile(conn) -> None:
    """Write the approved Carmeco strategy-session profile. Never touch Brown.

    Once Client Setup campaigns exist for Carmeco, campaigns are the source of
    truth — do not overwrite target_profiles from the hardcoded seed.
    """
    profile = DEFAULT_CLIENT_PROFILES["carmeco"]
    rows = conn.execute("SELECT id, code, name FROM clients").fetchall()
    for r in rows:
        code = _blank(r["code"]).lower()
        name = _blank(r["name"]).lower()
        if "carmeco" not in code and "carmeco" not in name:
            continue
        cid = int(r["id"])
        # Campaigns own the profile after Client Setup migration
        has_campaign = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='client_campaigns'"
        ).fetchone()
        if has_campaign:
            camp = conn.execute(
                "SELECT id FROM client_campaigns WHERE client_id = ? LIMIT 1",
                (cid,),
            ).fetchone()
            if camp:
                continue
        existing = conn.execute(
            "SELECT client_id FROM client_target_profiles WHERE client_id = ?",
            (cid,),
        ).fetchone()
        if not existing:
            conn.execute(
                f"""
                INSERT INTO client_target_profiles (
                    client_id, summary, target_industries, target_capabilities,
                    target_products, notes, {", ".join(PROFILE_EXTENDED_COLUMNS)}, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, {", ".join("?" for _ in PROFILE_EXTENDED_COLUMNS)}, ?)
                """,
                (
                    cid,
                    profile["summary"],
                    profile["target_industries"],
                    profile["target_capabilities"],
                    profile["target_products"],
                    profile["notes"],
                    *[profile.get(c, "") for c in PROFILE_EXTENDED_COLUMNS],
                    _now(),
                ),
            )
        else:
            conn.execute(
                f"""
                UPDATE client_target_profiles SET
                    summary = ?,
                    target_industries = ?,
                    target_capabilities = ?,
                    target_products = ?,
                    notes = ?,
                    {", ".join(f"{c} = ?" for c in PROFILE_EXTENDED_COLUMNS)},
                    updated_at = ?
                WHERE client_id = ?
                """,
                (
                    profile["summary"],
                    profile["target_industries"],
                    profile["target_capabilities"],
                    profile["target_products"],
                    profile["notes"],
                    *[profile.get(c, "") for c in PROFILE_EXTENDED_COLUMNS],
                    _now(),
                    cid,
                ),
            )


def _resolve_company(
    conn, *, company_id: int | None, external_record_no: str | None
) -> dict[str, Any]:
    if company_id:
        row = conn.execute(
            """
            SELECT id, company_name, external_record_no, website, address, city, state, zip,
                   type_of_industry, legacy_phone
            FROM companies WHERE id = ?
            """,
            (company_id,),
        ).fetchone()
        if row:
            return dict(row)
    rn = _blank(external_record_no)
    if rn:
        row = conn.execute(
            """
            SELECT id, company_name, external_record_no, website, address, city, state, zip,
                   type_of_industry, legacy_phone
            FROM companies WHERE external_record_no = ?
            """,
            (rn,),
        ).fetchone()
        if row:
            return dict(row)
        # Relationship record no → master company
        row = conn.execute(
            """
            SELECT co.id, co.company_name, co.external_record_no, co.website, co.address,
                   co.city, co.state, co.zip, co.type_of_industry, co.legacy_phone
            FROM client_company_relationships ccr
            JOIN companies co ON co.id = ccr.company_id
            WHERE ccr.external_record_no = ?
            LIMIT 1
            """,
            (rn,),
        ).fetchone()
        if row:
            return dict(row)
    raise LookupError("Company not found.")


def _authorized_relationships(
    conn, *, company_id: int, visible_ids: list[int]
) -> list[dict[str, Any]]:
    if not visible_ids:
        return []
    rows = conn.execute(
        f"""
        SELECT
            ccr.client_id,
            cl.name AS client_name,
            cl.code AS client_code,
            COALESCE(NULLIF(TRIM(ccr.external_record_no), ''), co.external_record_no)
                AS external_record_no,
            COALESCE(ccr.status, '') AS status,
            COALESCE(ccr.is_hot, 0) AS is_hot
        FROM client_company_relationships ccr
        JOIN clients cl ON cl.id = ccr.client_id
        JOIN companies co ON co.id = ccr.company_id
        WHERE ccr.company_id = ?
          AND ccr.client_id IN ({_placeholders(visible_ids)})
        ORDER BY cl.name COLLATE NOCASE
        """,
        [company_id, *visible_ids],
    ).fetchall()
    return [dict(r) for r in rows]


def _gather_northstar_known(
    conn,
    *,
    user_id: int,
    company: dict[str, Any],
    working_for_client_id: int,
    visible_ids: list[int],
    campaign_id: int | None = None,
) -> ResearchNorthStarKnown:
    company_id = int(company["id"])
    rels = _authorized_relationships(
        conn, company_id=company_id, visible_ids=visible_ids
    )
    working = next(
        (r for r in rels if int(r["client_id"]) == working_for_client_id), None
    )
    client_name = ""
    crow = conn.execute(
        "SELECT name FROM clients WHERE id = ?", (working_for_client_id,)
    ).fetchone()
    if crow:
        client_name = _blank(crow["name"])

    # Master contact population = all rows for company_id (no RN filter, no display LIMIT for dedupe).
    # Research UI shows a relevance-ranked sample only; contacts_total is authoritative.
    all_contact_rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id AS contact_id,
                   TRIM(COALESCE(first_name,'') || ' ' || COALESCE(last_name,'')) AS name,
                   COALESCE(title,'') AS title,
                   COALESCE(phone,'') AS phone,
                   COALESCE(email,'') AS email
            FROM contacts
            WHERE company_id = ?
            ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE
            """,
            (company_id,),
        ).fetchall()
    ]
    contacts_total = len(all_contact_rows)

    personas: list[str] = []
    try:
        personas, _src, _camp = _resolve_research_personas(
            conn,
            working_id=working_for_client_id,
            campaign_id=campaign_id,
        )
    except Exception:
        personas = []

    def _contact_relevance_rank(row: dict[str, Any]) -> tuple[int, str]:
        title = _blank(row.get("title"))
        name = _blank(row.get("name")).lower()
        if personas and title:
            try:
                from research_people import classify_persona_relevance

                tier, _persona, _why = classify_persona_relevance(title, personas)
                tier_rank = {
                    "HIGH": 0,
                    "MEDIUM": 1,
                    "LOW": 2,
                    "NOT_TARGET": 3,
                }.get((tier or "").upper(), 4)
                return (tier_rank, name)
            except Exception:
                pass
        # Fallback: prefer buying-role titles when personas unavailable
        t = title.lower()
        if any(
            k in t
            for k in (
                "commodity",
                "buyer",
                "purchas",
                "procurement",
                "sourcing",
                "supply",
            )
        ):
            return (0, name)
        if any(k in t for k in ("engineer", "manager", "director")):
            return (1, name)
        return (2, name)

    ranked = sorted(all_contact_rows, key=_contact_relevance_rank)
    display_limit = 12
    contacts = ranked[:display_limit]

    notes: list[dict] = []
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_notes'"
    ).fetchone():
        notes = [
            {
                "excerpt": _blank(r["note_text"])[:280],
                "client_name": _blank(r["client_name"]),
                "created_at": _blank(r["created_at"]),
            }
            for r in conn.execute(
                f"""
                SELECT ln.note_text, ln.created_at, COALESCE(cl.name,'') AS client_name
                FROM legacy_notes ln
                LEFT JOIN clients cl ON cl.id = ln.client_id
                WHERE ln.company_id = ?
                  AND (ln.client_id IS NULL OR ln.client_id IN ({_placeholders(visible_ids)}))
                ORDER BY ln.created_at DESC
                LIMIT 8
                """,
                [company_id, *visible_ids],
            ).fetchall()
            if _blank(r["note_text"])
        ]

    activities: list[dict] = []
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='activities'"
    ).fetchone():
        activities = [
            {
                "activity_type": _blank(r["activity_type"]),
                "activity_at": _blank(r["activity_at"]),
                "client_name": _blank(r["client_name"]),
                "notes": _blank(r["notes"])[:200],
            }
            for r in conn.execute(
                f"""
                SELECT a.activity_type, a.activity_at, a.notes, cl.name AS client_name
                FROM activities a
                JOIN clients cl ON cl.id = a.client_id
                WHERE a.company_id = ?
                  AND a.client_id IN ({_placeholders(visible_ids)})
                ORDER BY a.activity_at DESC
                LIMIT 10
                """,
                [company_id, *visible_ids],
            ).fetchall()
        ]

    milestones: list[dict] = []
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='revenue_milestones'"
    ).fetchone():
        milestones = [
            {
                "milestone_type": _blank(r["milestone_type"]),
                "milestone_date": _blank(r["milestone_date"]),
                "client_name": _blank(r["client_name"]),
            }
            for r in conn.execute(
                f"""
                SELECT rm.milestone_type, rm.milestone_date, cl.name AS client_name
                FROM revenue_milestones rm
                JOIN clients cl ON cl.id = rm.client_id
                WHERE rm.company_id = ?
                  AND rm.client_id IN ({_placeholders(visible_ids)})
                ORDER BY rm.milestone_date DESC
                """,
                [company_id, *visible_ids],
            ).fetchall()
        ]

    cross_client: list[dict] = []
    opp_score = None
    try:
        listed = list_cross_client_opportunities(
            user_id,
            target_client_id=working_for_client_id,
            include_dismissed=True,
        )
        opp = next(
            (o for o in listed.opportunities if o.company_id == company_id), None
        )
        if opp:
            opp_score = opp.opportunity_score
            source_names = list(getattr(opp, "source_client_names", None) or [])
            cross_client.append(
                {
                    "role": "Cross-client opportunity for Working For client",
                    "client_name": client_name,
                    "opportunity_score": opp.opportunity_score,
                    "signals": list(opp.signal_types or []),
                    "source_clients": source_names,
                    "attribution": (
                        f"Signals attributed to: {', '.join(source_names)}"
                        if source_names
                        else "Signals from other authorized NorthStar clients"
                    ),
                }
            )
    except Exception:
        pass

    for r in rels:
        if int(r["client_id"]) == working_for_client_id:
            continue
        mil_for = [
            m["milestone_type"]
            for m in milestones
            if m["client_name"] == _blank(r["client_name"])
        ]
        cross_client.append(
            {
                "role": "Other authorized client history",
                "client_name": _blank(r["client_name"]),
                "external_record_no": _blank(r["external_record_no"]),
                "status": _blank(r["status"]),
                "is_hot": bool(r["is_hot"]),
                "signals": mil_for,
            }
        )

    sales_events: list[dict] = []
    try:
        from sales_events_intel import event_to_item, fetch_sales_events

        se_rows = fetch_sales_events(
            visible_client_ids=visible_ids,
            company_id=company_id,
            limit=50,
        )
        for r in se_rows:
            item = event_to_item(r)
            cid = int(r.get("client_id") or 0)
            if cid and cid != working_for_client_id:
                item["cross_client_label"] = f"{_blank(r.get('client_name'))} History"
                item["attribution"] = (
                    f"{_blank(r.get('client_name'))} History — not owned by {client_name}."
                )
                for row in cross_client:
                    if _blank(row.get("client_name")) == _blank(r.get("client_name")):
                        sigs = list(row.get("sales_event_signals") or [])
                        et = _blank(r.get("event_type"))
                        if et and et not in sigs:
                            sigs.append(et)
                        row["sales_event_signals"] = sigs
            sales_events.append(item)
    except Exception:
        sales_events = []

    prev = [
        {
            "research_run_id": int(r["id"]),
            "completed_at": _blank(r["completed_at"] or r["started_at"]),
            "initiated_by": _blank(r["initiated_by_name"]),
            "summary": _blank(r["summary"])[:240],
            "working_for_client_id": int(r["working_for_client_id"]),
        }
        for r in conn.execute(
            """
            SELECT id, completed_at, started_at, initiated_by_name, summary,
                   working_for_client_id
            FROM company_research_runs
            WHERE company_id = ?
            ORDER BY COALESCE(completed_at, started_at) DESC
            LIMIT 8
            """,
            (company_id,),
        ).fetchall()
    ]

    return ResearchNorthStarKnown(
        company_name=_blank(company["company_name"]),
        master_record_no=_blank(company["external_record_no"]),
        website=_blank(company["website"]),
        city=_blank(company["city"]),
        state=_blank(company["state"]),
        address=_blank(company["address"]),
        working_for_client_id=working_for_client_id,
        working_for_client_name=client_name,
        working_for_record_no=_blank(working["external_record_no"]) if working else "",
        working_for_status=_blank(working["status"]) if working else "New",
        has_relationship=working is not None,
        contacts=contacts,
        contacts_total=contacts_total,
        milestones=milestones,
        notes=notes,
        activities=activities,
        sales_events=sales_events,
        cross_client=cross_client,
        previous_research=prev,
        opportunity_score=opp_score,
    )


def _values_equivalent(a: str, b: str, field_key: str) -> bool:
    left = _blank(a).lower().rstrip("/")
    right = _blank(b).lower().rstrip("/")
    if not left or not right or right == NOT_VERIFIED.lower():
        return False
    if field_key == "website":
        return normalize_website(left).lower() == normalize_website(right).lower()
    return left == right


def _split_profile_list(raw: str) -> list[str]:
    text = _blank(raw)
    if not text:
        return []
    parts: list[str] = []
    for chunk in text.replace("\n", ";").split(";"):
        for piece in chunk.split(","):
            item = piece.strip().strip("-").strip()
            if item:
                parts.append(item)
    return parts


def _profile_assessment(profile: dict[str, str] | None) -> tuple[bool, list[str], list[str]]:
    """Return (incomplete, fields_present, gaps).

    Strong Fit requires the approved ICP fields — not inventable when blank.
    company_size_preferences may remain empty when no preference is established.
    """
    profile = profile or {}
    present: list[str] = []
    core_keys = (
        "summary",
        "target_industries",
        "target_capabilities",
        "target_products",
        "notes",
        *PROFILE_EXTENDED_COLUMNS,
    )
    for key in core_keys:
        if _blank(profile.get(key)):
            present.append(key)

    gaps: list[str] = []
    for key in PROFILE_STRONG_FIT_REQUIRED:
        if not _blank(profile.get(key)):
            gaps.append(key)

    # Optional / explicitly unset — report but do not block completeness
    if not _blank(profile.get("company_size_preferences")):
        # Not a Strong Fit blocker; informational only when absent
        pass
    if not _blank(profile.get("geographic_preferences")):
        if "geographic_preferences" not in gaps:
            gaps.append("geographic_preferences")
    if not _blank(profile.get("production_preference")):
        gaps.append("production_preference")
    if not _blank(profile.get("stamping_capability")) and "stamp" in _blank(
        profile.get("primary_service")
    ).lower():
        gaps.append("stamping_capability")

    incomplete = bool(
        [g for g in gaps if g in PROFILE_STRONG_FIT_REQUIRED]
    )
    return incomplete, present, gaps


def _campaign_criteria_configured(profile: dict[str, str] | None) -> bool:
    """True when the campaign/profile has usable fit criteria (not a thin Default shell).

    Thin seed rows with only industries/products/notes are not enough to rate fit.
    Engagement history never counts as campaign criteria.
    """
    profile = profile or {}
    if _blank(profile.get("primary_service")):
        return True
    if _blank(profile.get("manufacturing_processes_sought")):
        return True
    if _blank(profile.get("fit_weighting_notes")):
        return True
    if _blank(profile.get("positive_fit_signals")) and _blank(
        profile.get("negative_fit_signals")
    ):
        return True
    if _blank(profile.get("secondary_services")) and _blank(
        profile.get("ideal_customer_types")
    ):
        return True
    return False


def _text_has_stamping(text: str) -> bool:
    """True only for meaningful stamping / stamped-component language — not generic metalwork."""
    t = text.lower()
    # Explicit stamping / die / draw language
    stamps = (
        "stamp",
        "stamping",
        "stamped",
        "stampings",
        "press stamp",
        "progressive die",
        "transfer die",
        "blanking die",
        "deep draw",
        "deep-drawn",
        "drawn metal",
        "press formed",
        "press-formed",
        "metal forming die",
        "stamped metal",
        "stamped component",
        "stamped part",
        "formed production metal",
        "formed metal component",
    )
    if any(x in t for x in stamps):
        # Guard: "rubber stamp" / marketing "stamp of approval" style noise
        if "rubber stamp" in t or "stamp of approval" in t or "time stamp" in t:
            return False
        return True
    return False


def _text_has_recurring_stamping_signal(text: str) -> bool:
    t = text.lower()
    if not _text_has_stamping(t):
        # Recurring alone is not enough without stamping context
        return False
    return any(
        x in t
        for x in (
            "recurring",
            "production program",
            "production volume",
            "high volume",
            "ongoing production",
            "serial production",
            "production run",
        )
    )


def _text_has_outsourcing_stamping_signal(text: str) -> bool:
    t = text.lower()
    if not (_text_has_stamping(t) or "metal component" in t or "stamped" in t):
        return False
    return any(
        x in t
        for x in (
            "outsourc",
            "contract stamp",
            "supplier of stamp",
            "purchase stamp",
            "buy stamp",
            "source stamp",
            "sourced stamp",
            "stamp supplier",
            "stamping supplier",
        )
    )


def _text_has_tooling_stamping_signal(text: str) -> bool:
    t = text.lower()
    return any(
        x in t
        for x in (
            "existing tooling",
            "transferred tooling",
            "transfer tooling",
            "tooling transfer",
            "stamping die",
            "progressive die tooling",
            "owned tooling",
            "customer-owned tooling",
            "customer owned tooling",
        )
    )


def _text_has_weak_stamping_alignment(text: str) -> bool:
    """Explicit negative alignment — missing info alone must NOT trigger this."""
    t = text.lower()
    return any(
        x in t
        for x in (
            "one-off only",
            "one off only",
            "prototype only",
            "custom only",
            "no recurring",
            "no stamped",
            "does not stamp",
            "do not stamp",
            "no stamping",
            "not a production",
            "job shop only",
        )
    )


def _text_has_secondary_service(text: str, secondary_services: list[str]) -> bool:
    t = text.lower()
    stems = []
    for s in secondary_services:
        sl = s.lower()
        if "laser" in sl:
            stems.append("laser")
        if "weld" in sl:
            stems.append("weld")
        if "fabricat" in sl:
            stems.append("fabricat")
        if "machin" in sl:
            stems.append("machin")
        if "paint" in sl:
            stems.append("paint")
        if "logistic" in sl:
            stems.append("logistic")
    if not stems:
        # Fallback secondary stems when profile lists them generically
        stems = ["laser", "weld", "fabricat", "machin", "paint"]
    return any(stem in t for stem in stems)


def _industry_match(verified: str, targets: list[str]) -> bool:
    v = verified.lower()
    for t in targets:
        tl = t.lower()
        if len(tl) < 3:
            continue
        if tl in v or v in tl:
            return True
        # Soft synonym bridges from approved Carmeco industries
        if "transport" in tl and ("transport" in v or "trailer" in v or "truck" in v):
            return True
        if "truck" in tl and ("truck" in v or "trailer" in v or "transport" in v):
            return True
        if "automot" in tl and "automot" in v:
            return True
        if "appliance" in tl and "appliance" in v:
            return True
        if "hvac" in tl and "hvac" in v:
            return True
    return False


def _compute_fit(
    *,
    client_name: str,
    client_id: int,
    profile: dict[str, str] | None,
    known: ResearchNorthStarKnown,
    verified_caps: list[str],
    verified_products: list[str],
    verified_inds: list[str],
    campaign_id: int | None = None,
    campaign_name: str = "",
    research_text_blobs: list[str] | None = None,
) -> ResearchFitView:
    """Evidence-based fit with stamping-primary weighting when profile defines it."""
    evidence: list[str] = []
    chains: list[dict] = []
    concerns: list[str] = []
    missing: list[str] = []
    opportunity = ""

    incomplete, fields_used, profile_gaps = _profile_assessment(profile)
    profile = profile or {}

    primary_service = _blank(profile.get("primary_service"))
    secondary_services = _split_profile_list(profile.get("secondary_services") or "")
    stamping_primary = "stamp" in primary_service.lower()
    target_inds = _split_profile_list(profile.get("target_industries") or "")
    positive_signals = _split_profile_list(profile.get("positive_fit_signals") or "")
    weighting = _blank(profile.get("fit_weighting_notes"))

    # Cross-client commercial history — confidence booster only
    other_signals: list[str] = []
    source_clients: list[str] = []
    for row in known.cross_client:
        sigs = [str(s) for s in (row.get("signals") or []) if s]
        cname = _blank(row.get("client_name"))
        if sigs and cname and cname != client_name:
            other_signals.extend(sigs)
            source_clients.append(cname)
            evidence.append(
                f"NORTHSTAR CROSS-CLIENT EXPERIENCE — {cname}: " + ", ".join(sigs)
            )
            chains.append(
                {
                    "fact": f"{cname} history includes {', '.join(sigs)}",
                    "why": (
                        f"Shows NorthStar has meaningful commercial history with this company, "
                        f"but does not by itself prove manufacturing fit for {client_name}."
                    ),
                    "kind": "cross_client",
                }
            )
    if known.opportunity_score is not None:
        evidence.append(
            f"Opportunity score {known.opportunity_score} "
            f"(confidence signal for {client_name}; not manufacturing proof)"
        )
        chains.append(
            {
                "fact": f"Opportunity score {known.opportunity_score}",
                "why": (
                    f"Increases confidence that {client_name} should review the account, "
                    "but alone cannot establish Strong Fit."
                ),
                "kind": "cross_client",
            }
        )

    # Own-client commercial history (Working For client milestones)
    own_milestones = sorted(
        {
            _blank(m.get("milestone_type"))
            for m in (known.milestones or [])
            if _blank(m.get("client_name")) == client_name
        }
        - {""}
    )
    if not own_milestones:
        for row in known.cross_client:
            if _blank(row.get("client_name")) == client_name and row.get("signals"):
                own_milestones = [str(s) for s in row["signals"] if s]
                break

    has_own_history = bool(own_milestones)
    history_work_type_known = False
    history_blob = ""
    if has_own_history:
        evidence.append(
            f"Previous {client_name} commercial history: " + ", ".join(own_milestones)
        )
        # Distinguish commercial success from current Stamping-campaign profile match.
        # Do not invent what was purchased/quoted unless stored history states it.
        history_blob = " ".join(
            [
                _blank(m.get("milestone_type"))
                + " "
                + _blank(m.get("label"))
                + " "
                + _blank(m.get("notes"))
                for m in (known.milestones or [])
                if _blank(m.get("client_name")) == client_name
            ]
        ).lower()
        history_work_type_known = _text_has_stamping(history_blob)
        if stamping_primary and not history_work_type_known:
            missing.append(
                f"Type of prior {client_name} work (stamping vs fabrication/other) is not "
                "identified in stored commercial history — PO/Quote/WebLead alone do not "
                "prove a current Stamping-campaign match."
            )
            concerns.append(
                f"NorthStar has successfully done business with this company for {client_name}, "
                "but stored history does not identify whether that work was metal stamping or "
                "related recurring stamped production. Commercial history must not manufacture "
                "a Strong Fit for the Stamping campaign."
            )
        chains.append(
            {
                "fact": f"{client_name} history includes {', '.join(own_milestones)}",
                "why": (
                    (
                        "Prior commercial success is important evidence and matches the positive "
                        "signal 'previous successful commercial history'. It increases confidence "
                        "to pursue the account, but PO / Quote / WebLead alone do not automatically "
                        "produce Possible Fit or Strong Fit for the Stamping campaign. "
                        + (
                            "Stored history indicates stamping-related prior work."
                            if history_work_type_known
                            else (
                                "The type of purchased/quoted work cannot be determined from "
                                "stored NorthStar history — do not invent it."
                            )
                        )
                    )
                    if stamping_primary
                    else "Previous commercial history increases confidence."
                ),
                "kind": "own_history",
            }
        )

    real_caps = [
        c
        for c in verified_caps
        if "not explicitly stated" not in c.lower() and c.lower() != "trailers"
    ]

    # Scan caps, products, and other verified research text for stamping signals
    scan_blobs = [
        *real_caps,
        *verified_products,
        *(research_text_blobs or []),
    ]
    combined_research = " ".join(scan_blobs)

    stamping_hits = [c for c in real_caps if _text_has_stamping(c)] + [
        p for p in verified_products if _text_has_stamping(p)
    ]
    for blob in research_text_blobs or []:
        b = _blank(blob)
        if b and _text_has_stamping(b) and b not in stamping_hits:
            stamping_hits.append(b[:160])

    secondary_hits = [
        c
        for c in real_caps
        if _text_has_secondary_service(c, secondary_services)
        and not _text_has_stamping(c)
    ]
    ind_hits = [i for i in verified_inds if _industry_match(i, target_inds)]

    has_stamping_evidence = bool(stamping_hits) or history_work_type_known
    has_secondary_alignment = bool(secondary_hits)
    has_recurring_stamping_opportunity = _text_has_recurring_stamping_signal(
        combined_research
    ) or (
        history_work_type_known
        and any(
            x in history_blob
            for x in ("recurring", "production", "program", "volume")
        )
        if has_own_history
        else False
    )
    has_outsourcing_stamping_evidence = _text_has_outsourcing_stamping_signal(
        combined_research
    )
    has_tooling_stamping_evidence = _text_has_tooling_stamping_signal(combined_research)
    has_weak_negative = _text_has_weak_stamping_alignment(combined_research)

    if has_stamping_evidence:
        stamp_label = (
            ", ".join(stamping_hits)
            if stamping_hits
            else f"prior {client_name} history tied to stamping"
        )
        evidence.append("Verified stamping / stamped-component evidence: " + stamp_label)
        for s in stamping_hits:
            chains.append(
                {
                    "fact": f"Verified stamping-related finding: {s}",
                    "why": (
                        f"Aligns with {client_name} primary service ({primary_service or 'stamping'})."
                    ),
                    "kind": "profile_alignment",
                }
            )
        if history_work_type_known and not stamping_hits:
            chains.append(
                {
                    "fact": f"Stored {client_name} history indicates stamping-related prior work",
                    "why": (
                        "History tied to stamping is direct campaign evidence; still confirm "
                        "current recurring/outsourced need."
                    ),
                    "kind": "own_history",
                }
            )
    if has_secondary_alignment:
        evidence.append(
            "Verified secondary-service alignment (not primary stamping): "
            + ", ".join(secondary_hits)
        )
        for s in secondary_hits:
            chains.append(
                {
                    "fact": f"Verified capability: {s}",
                    "why": (
                        f"Relates to {client_name} secondary services "
                        f"({'; '.join(secondary_services) or 'fabrication/welding/etc.'}). "
                        + (
                            "Per weighting rules, generic fab/cut/weld alone cannot establish "
                            "Possible Fit or Strong Fit when stamping is primary."
                            if stamping_primary
                            else f"Supports alignment with {client_name}."
                        )
                    ),
                    "kind": "profile_alignment",
                }
            )
    if ind_hits:
        evidence.append("Verified industries/markets: " + ", ".join(ind_hits))
        for i in ind_hits:
            chains.append(
                {
                    "fact": f"Verified market/industry mention: {i}",
                    "why": (
                        f"Appears in {client_name} target industries. Industry alone is not "
                        "a meaningful stamping/stamped-component signal."
                        if stamping_primary
                        else f"Appears in {client_name} target industries."
                    ),
                    "kind": "profile_alignment",
                }
            )

    if weighting and stamping_primary:
        evidence.append(
            "Profile weighting: stamping is primary; generic fab/cut/weld/industry "
            "alignment is weaker and does not create Possible Fit by itself."
        )

    # Geography — soft preference only
    geo = _blank(profile.get("geographic_preferences"))
    if geo:
        fields_used = list(dict.fromkeys([*fields_used, "geographic_preferences"]))
        if known.city and known.state:
            chains.append(
                {
                    "fact": f"NorthStar master location on file: {known.city}, {known.state}",
                    "why": (
                        f"{client_name} geography preference is soft ({geo}). "
                        "Not used as an absolute exclusion."
                    ),
                    "kind": "geography",
                }
            )
        else:
            missing.append(
                "Company location not verified publicly; geographic preference not fully evaluable."
            )

    # Missing / unverified criteria (salesperson next questions)
    if stamping_primary and not has_stamping_evidence:
        missing.append(
            "No meaningful stamping or stamped/formed-component signal verified from "
            "public research (do not infer stamping solely from product type, "
            "cutting, fabrication, welding, or industry)."
        )
        missing.append(
            "To reach Possible Fit: verify the company uses or likely requires stamped/"
            "formed production metal components (e.g. stamped parts called out in "
            "manufacturing/sourcing, progressive die, existing stamped components)."
        )
        missing.append(
            "To reach Strong Fit: also verify a realistic recurring/outsourced stamping "
            "opportunity (outsourcing, tooling transfer, recurring program/volume, or "
            f"stamping-tied {client_name} history)."
        )
    if stamping_primary and has_stamping_evidence:
        if not has_outsourcing_stamping_evidence:
            missing.append(
                "Whether stamped/formed components are outsourced (vs made internally) "
                "is not verified."
            )
        if not has_recurring_stamping_opportunity:
            missing.append(
                "Recurring / ongoing stamped-component volume or production program "
                "not verified."
            )
        if not has_tooling_stamping_evidence:
            missing.append("Existing / transferred tooling for stamped parts not identified.")
        missing.append(
            "Current supplier situation / sourcing responsibility not verified publicly."
        )
        missing.append(
            "New program / overflow / capacity-constraint signals not verified publicly."
        )
    if not real_caps:
        missing.append(
            "Manufacturing processes not publicly stated on reviewed pages "
            "(do not infer processes from product type alone)."
        )
    if not verified_products:
        missing.append("Major product categories not clearly verified from public pages.")
    missing.append("Purchasing/procurement contact not identified in public research.")
    missing.append(
        "Specific facility responsible for production not fully verified beyond public mentions."
    )

    if incomplete:
        concerns.append(
            f"{client_name} target profile is incomplete for Strong Fit decisions. "
            "Richer ICP fields are not defined yet."
        )
        for g in profile_gaps:
            if g in PROFILE_STRONG_FIT_REQUIRED:
                missing.append(f"Client target profile missing: {g.replace('_', ' ')}.")

    if not known.has_relationship:
        concerns.append(
            f"No existing {client_name} CRM relationship yet — soft-open / New context only."
        )

    # Potential opportunity — never claim they buy/outsource without evidence
    if has_stamping_evidence and stamping_primary:
        opportunity = (
            f"Potential Opportunity: stamping/stamped-component signal found. Confirm "
            "outsourcing, recurring volume, tooling, and sourcing before treating as "
            f"a Strong Fit for {client_name}."
        )
    elif has_own_history:
        opportunity = (
            f"Potential Opportunity: {client_name} has prior commercial engagement "
            f"({', '.join(own_milestones)}). Use as conversation context; do not assume "
            "current outsourced stamping demand without confirmation."
            if stamping_primary
            else (
                f"Potential Opportunity: prior {client_name} commercial engagement "
                f"({', '.join(own_milestones)})."
            )
        )
    elif other_signals or known.opportunity_score:
        opportunity = (
            f"Potential Opportunity: NorthStar cross-client history indicates prior commercial "
            f"engagement"
            + (f" ({', '.join(sorted(set(other_signals)))})" if other_signals else "")
            + f". Use as conversation context for {client_name}; do not assume current "
            "outsourcing needs without confirmation."
        )
    elif verified_products and stamping_primary:
        opportunity = (
            f"Potential Opportunity: public pages describe products "
            f"({', '.join(verified_products[:4])}), which may involve metal components — "
            f"stamping/stamped-component need not verified. Confirm before treating as demand."
        )

    has_cross_client = bool(other_signals) or (
        known.opportunity_score is not None and known.opportunity_score > 0
    )
    has_generic_manufacturing = (
        has_secondary_alignment
        or bool(ind_hits)
        or bool(verified_products)
        or bool(real_caps)
    )
    strong_stamping_support = (
        has_stamping_evidence
        and (
            has_recurring_stamping_opportunity
            or has_outsourcing_stamping_evidence
            or has_tooling_stamping_evidence
            or history_work_type_known
        )
    )

    # Empty / thin campaign ICP — never invent Campaign Fit; company research still stands.
    if not _campaign_criteria_configured(profile):
        missing.insert(
            0,
            "Campaign criteria not configured: set primary service, manufacturing "
            "processes sought, fit weighting notes, and/or positive and negative "
            "fit signals before rating campaign fit.",
        )
        concerns.append(
            f"{client_name} campaign targeting criteria are not configured. "
            "Public company research is available independently of Campaign Fit."
        )
        why = (
            f"Campaign criteria not configured for {client_name}"
            + (f" ({campaign_name})" if _blank(campaign_name) else "")
            + ". Configure primary service and fit signals to rate campaign fit. "
            "Company research findings remain available."
        )
        return ResearchFitView(
            client_id=client_id,
            client_name=client_name,
            campaign_id=campaign_id,
            campaign_name=campaign_name
            or _blank(profile.get("campaign_name")),
            fit_result=FIT_CRITERIA_NOT_CONFIGURED,
            why=why,
            supporting_evidence=evidence,
            evidence_chains=chains,
            potential_opportunity=opportunity,
            concerns=concerns,
            missing_information=missing,
            profile_fields_used=fields_used,
            profile_incomplete=True,
            profile_gaps=profile_gaps,
        )

    # --- Rating rules ---
    if stamping_primary:
        if has_weak_negative and not has_stamping_evidence:
            fit = "Weak Fit"
            why = (
                f"Available evidence indicates poor alignment with {client_name}'s "
                f"Stamping campaign (e.g. one-off/custom or no meaningful stamped-component "
                "opportunity), not merely missing information."
            )
        elif incomplete and not has_stamping_evidence:
            # Incomplete ICP cannot invent Possible Fit from generic manufacturing
            if has_generic_manufacturing or has_own_history or has_cross_client:
                fit = "Insufficient Information"
                why = (
                    f"{client_name}'s target profile is incomplete and research has not "
                    "established a meaningful stamping or stamped/formed-component signal. "
                    "Generic manufacturing, cutting/fab/weld, industry, product type, or "
                    "commercial history alone is not Possible Fit."
                )
            else:
                fit = "Insufficient Information"
                why = (
                    f"Not enough verified manufacturing alignment or a complete {client_name} "
                    "target profile to rate fit."
                )
        elif strong_stamping_support and not incomplete:
            fit = "Strong Fit"
            why = (
                f"Meaningful evidence supports a realistic recurring/outsourced stamping "
                f"opportunity aligned with {client_name}'s primary service "
                f"({primary_service or 'stamping'})."
            )
        elif has_stamping_evidence:
            fit = "Possible Fit"
            why = (
                f"Meaningful stamping/stamped-component evidence exists for {client_name}, "
                "but an important element remains unverified (e.g. outsourcing, sourcing "
                "responsibility, supplier situation, current program, volume, or tooling)."
            )
            if has_secondary_alignment and not strong_stamping_support:
                concerns.append(
                    "Secondary-service alignment is present but is not what drives Possible "
                    "Fit — the stamping/stamped-component signal does."
                )
        elif has_generic_manufacturing or has_own_history or has_cross_client:
            fit = "Insufficient Information"
            why = (
                f"Research found manufacturing and/or commercial alignment for {client_name}, "
                "but not a meaningful stamping or stamped/formed-component signal. "
                "Generic fabrication, cutting, welding, product type, industry match, or "
                "prior commercial history alone does not justify Possible Fit for a "
                "stamping-primary campaign."
            )
            if has_secondary_alignment:
                concerns.append(
                    "Secondary-service alignment only (e.g. fabrication/cutting/welding) — "
                    "insufficient for Possible Fit under stamping-primary weighting."
                )
            if has_own_history or has_cross_client:
                concerns.append(
                    "Commercial history increases confidence to review the account but does "
                    "not create a stamping-campaign Possible Fit without a stamping signal."
                )
        else:
            fit = "Insufficient Information"
            why = (
                f"Not enough reliable evidence to evaluate stamping fit for {client_name}."
            )
    elif incomplete:
        if has_stamping_evidence or has_secondary_alignment or has_own_history or has_cross_client or verified_products:
            fit = "Possible Fit"
            why = (
                f"{client_name}'s stored target profile is incomplete, and/or public research "
                "has not verified primary-process alignment needed for Strong Fit."
            )
        else:
            fit = "Insufficient Information"
            why = (
                f"Not enough verified manufacturing alignment or a complete {client_name} "
                "target profile to rate fit."
            )
    elif has_weak_negative and not (has_stamping_evidence or has_secondary_alignment):
        fit = "Weak Fit"
        why = (
            f"Available evidence indicates poor alignment with {client_name}'s stored "
            "target profile (negative signals present — not merely missing information)."
        )
    elif has_stamping_evidence or has_secondary_alignment:
        fit = "Possible Fit"
        why = (
            f"Manufacturing alignment with {client_name} exists, but criteria for Strong Fit "
            "are not fully met."
        )
    elif has_own_history or has_cross_client:
        fit = "Insufficient Information"
        why = (
            f"Commercial history increases confidence for {client_name} review, but "
            "manufacturing-process alignment is not verified."
        )
    elif evidence:
        fit = "Insufficient Information"
        why = (
            f"Limited verified information; not enough to confirm alignment with "
            f"{client_name}'s stored target profile. Missing information is not treated "
            "as negative evidence."
        )
    else:
        fit = "Insufficient Information"
        why = f"Not enough verified information to evaluate fit for {client_name}."

    return ResearchFitView(
        client_id=client_id,
        client_name=client_name,
        campaign_id=campaign_id,
        campaign_name=campaign_name
        or _blank(profile.get("campaign_name")),
        fit_result=fit,
        why=why,
        supporting_evidence=evidence,
        evidence_chains=chains,
        potential_opportunity=opportunity,
        concerns=concerns,
        missing_information=missing,
        profile_fields_used=fields_used,
        profile_incomplete=incomplete,
        profile_gaps=profile_gaps,
    )


def _build_summary(
    *,
    company_name: str,
    known: ResearchNorthStarKnown,
    fit: ResearchFitView,
    verified_products: list[str],
    verified_caps: list[str],
    pages_count: int,
    verified_inds: list[str] | None = None,
    overview_bits: list[str] | None = None,
    website: str = "",
    locations: list[str] | None = None,
    stamping_primary: bool = False,
) -> str:
    """Company-profile-first research summary; campaign fit is a separate rating."""
    parts: list[str] = []
    client = known.working_for_client_name or "this client"
    camp = _blank(fit.campaign_name) or "Default"
    inds = [i for i in (verified_inds or []) if _blank(i)]
    locs = [loc for loc in (locations or []) if _blank(loc)]
    overviews = [o for o in (overview_bits or []) if _blank(o)]
    fit_key = _blank(fit.fit_result).lower()
    criteria_missing = (
        _blank(fit.fit_result) == FIT_CRITERIA_NOT_CONFIGURED
        or "criteria not configured" in fit_key
        or "criteria not configured" in _blank(fit.why).lower()
    )

    ns_loc = ", ".join([x for x in [known.city, known.state] if x])
    identity = f"1) Company identity: {company_name}"
    if _blank(website):
        identity += f" · website {_blank(website)}"
    if locs:
        identity += f" · locations {', '.join(locs[:4])}"
    elif ns_loc:
        identity += f" · NorthStar location {ns_loc}"
    parts.append(identity + ".")

    if overviews:
        parts.append(f"2) Overview: {overviews[0][:280]}")
    elif verified_products:
        parts.append(
            f"2) Manufactures / offers: {', '.join(verified_products[:6])}."
        )
    else:
        parts.append(
            f"2) Product categories were not clearly phrase-verified from {pages_count} "
            f"reviewed public page(s); see overview/industries when available."
        )

    if inds:
        parts.append(f"Industries / markets mentioned: {', '.join(inds[:6])}.")

    real_caps = [
        c for c in verified_caps if "not explicitly stated" not in c.lower()
    ]
    if real_caps:
        parts.append(
            f"3) Manufacturing evidence found: {', '.join(real_caps[:6])}."
        )
    elif verified_products:
        parts.append(
            f"3) Product categories verified: {', '.join(verified_products[:6])} "
            "(manufacturing processes not explicitly stated on reviewed pages)."
        )
    else:
        parts.append(
            "3) Manufacturing processes were not explicitly stated on the reviewed public pages."
        )

    cross_bits: list[str] = []
    for row in known.cross_client:
        cname = _blank(row.get("client_name"))
        sigs = [str(s) for s in (row.get("signals") or []) if s]
        if cname and sigs and cname != known.working_for_client_name:
            cross_bits.append(f"{cname} ({', '.join(sigs)})")
    if cross_bits:
        parts.append(
            "NorthStar commercial context (engagement/confidence only): prior history via "
            + "; ".join(cross_bits)
            + (
                f"; opportunity score {known.opportunity_score}."
                if known.opportunity_score is not None
                else "."
            )
        )

    if criteria_missing:
        parts.append(
            f"4) Campaign fit ({client} / {camp}): campaign criteria not configured — "
            "configure primary service and fit signals to rate fit. Company research above "
            "is independent of Campaign Fit."
        )
    elif stamping_primary:
        stamp_bits = [
            e
            for e in (fit.supporting_evidence or [])
            if ("stamp" in e.lower() or "stamped" in e.lower())
            and "weaker" not in e.lower()
            and "weighting" not in e.lower()
        ]
        if stamp_bits:
            parts.append(
                f"4) Campaign connection ({client} / {camp}): " + stamp_bits[0]
            )
        elif "insufficient" in fit_key:
            parts.append(
                f"4) Campaign connection ({client} / {camp}): no meaningful stamping or "
                "stamped/formed-component signal verified. Generic manufacturing capability "
                "is not treated as campaign fit."
            )
        else:
            align = [
                e
                for e in (fit.supporting_evidence or [])
                if "alignment" in e.lower() or "industr" in e.lower()
            ]
            parts.append(
                f"4) Campaign connection ({client} / {camp}): "
                + (align[0] if align else "limited verified campaign-specific alignment.")
            )
    else:
        align = [
            e
            for e in (fit.supporting_evidence or [])
            if "alignment" in e.lower() or "industr" in e.lower() or e.startswith("Verified")
        ]
        if "insufficient" in fit_key:
            parts.append(
                f"4) Campaign connection ({client} / {camp}): not enough verified alignment "
                f"with stored campaign criteria to rate fit yet."
            )
        else:
            parts.append(
                f"4) Campaign connection ({client} / {camp}): "
                + (align[0] if align else _blank(fit.why) or "see Campaign Fit rating.")
            )

    if fit.missing_information:
        gaps = [g for g in fit.missing_information if g][:3]
        parts.append("5) Important evidence still missing: " + " | ".join(gaps))
    else:
        parts.append("5) Important evidence still missing: none listed from this run.")

    parts.append(f"6) Rating: {fit.fit_result} — {fit.why}")
    return " ".join(parts)


# Engagement signals that raise opportunity confidence — never Campaign Fit by themselves.
_ENGAGEMENT_SIGNAL_RANK = {
    "Purchase Order": 100,
    "Quote": 80,
    "RFQ": 75,
    "Appointment Set": 70,
    "WebLead": 40,
    "Hot": 35,
    "Send Information": 25,
}


def _build_decision_summary(
    *,
    company_name: str,
    client_name: str,
    campaign_name: str,
    fit: ResearchFitView | None,
    verified_products: list[str],
    verified_caps: list[str],
    overview_bits: list[str] | None = None,
) -> ResearchDecisionSummary:
    """Concise sales-facing Campaign Fit block — presentation only."""
    client = _blank(client_name) or "this client"
    camp = _blank(campaign_name) or _blank((fit.campaign_name if fit else "") or "Default")
    fit_result = _blank(fit.fit_result if fit else "") or "Insufficient Information"
    key = fit_result.lower()
    products = [p for p in verified_products if _blank(p)]
    caps = [
        c
        for c in verified_caps
        if _blank(c) and "not explicitly stated" not in c.lower()
    ]
    overview = " ".join(overview_bits or []).lower()
    looks_manufacturer = bool(products or caps) or any(
        x in overview for x in ("manufactur", "oem", "fabricat", "plant", "production")
    )
    stamping_campaign = "stamp" in camp.lower() or (
        "stamp" in _blank(fit.why if fit else "").lower()
    )

    if "strong" in key:
        why = (
            f"NorthStar found meaningful evidence of a realistic recurring or outsourced "
            f"stamping opportunity aligned with {client}'s {camp} campaign."
            if stamping_campaign
            else (
                f"NorthStar found meaningful evidence supporting a strong match for "
                f"{client}'s {camp} campaign."
            )
        )
        still = (
            "Confirm current sourcing responsibility, open programs, and next-step contacts "
            "before advancing the opportunity."
        )
    elif "possible" in key:
        why = (
            f"NorthStar found a meaningful stamping or stamped/formed-component signal for "
            f"{company_name}, but an important element remains unverified "
            f"(such as outsourcing, tooling, volume, sourcing, or current program)."
            if stamping_campaign
            else (
                f"NorthStar found meaningful campaign-relevant evidence for {company_name}, "
                "but an important element remains unverified."
            )
        )
        still = (
            "Determine whether the company purchases stamped components, has existing "
            "stamping tooling, outsources stamping, or has upcoming programs requiring "
            "stamped parts."
            if stamping_campaign
            else (
                "Confirm the remaining unverified campaign criteria before treating this "
                "as a strong opportunity."
            )
        )
    elif "weak" in key:
        why = (
            f"Available evidence suggests poor alignment with {client}'s {camp} campaign — "
            "not merely missing information."
        )
        still = (
            "Revisit only if new evidence shows a realistic production stamped-component "
            "opportunity."
            if stamping_campaign
            else "Revisit only if new evidence shows stronger campaign alignment."
        )
    elif "criteria not configured" in key:
        why = (
            f"Campaign criteria not configured for {client}'s {camp} campaign. "
            f"Public company research for {company_name} is still available; configure "
            "primary service and fit signals before rating Campaign Fit."
        )
        still = (
            "Configure campaign targeting criteria (primary service, processes sought, "
            "fit weighting, positive/negative signals), then refresh research to rate fit."
        )
    else:
        # Insufficient Information (and default)
        if looks_manufacturer and stamping_campaign:
            why = (
                f"NorthStar verified that {company_name} is a manufacturing company, "
                "but current public research did not establish that it uses or outsources "
                "stamped/formed production metal components."
            )
        elif looks_manufacturer:
            why = (
                f"NorthStar verified manufacturing activity for {company_name}, but current "
                f"public research did not establish a clear match to {client}'s {camp} campaign."
            )
        else:
            why = (
                f"Current public research did not establish enough evidence to rate "
                f"{company_name} for {client}'s {camp} campaign."
            )
        still = (
            "Determine whether the company purchases stamped components, has existing "
            "stamping tooling, outsources stamping, or has upcoming programs requiring "
            "stamped parts."
            if stamping_campaign
            else (
                "Gather the missing campaign-specific evidence listed under Missing Information "
                "before upgrading the rating."
            )
        )

    # Prefer a short actionable still-need from stored missing_information when present
    if fit and fit.missing_information:
        for g in fit.missing_information:
            gl = _blank(g).lower()
            if not gl:
                continue
            if "to reach possible" in gl or "to reach strong" in gl:
                # Keep the sales-facing template above; those lines are already covered
                continue
            if "purchasing" in gl or "facility" in gl:
                continue
            if stamping_campaign and (
                "stamp" in gl or "tooling" in gl or "outsourc" in gl or "recurring" in gl
            ):
                # Keep the concise sales template — more readable than technical gap lines
                break

    return ResearchDecisionSummary(
        client_name=client,
        campaign_name=camp,
        fit_result=fit_result,
        why=why,
        still_need=still,
    )


def _normalize_engagement_signal(raw: str) -> str:
    t = _blank(raw)
    if not t:
        return ""
    low = t.lower()
    if low in {"appointment", "appointment set", "appt set", "appt"}:
        return "Appointment Set"
    if "appointment scheduled" in low or "appointment rescheduled" in low or "appointment completed" in low:
        return "Appointment Set"
    if "purchase order" in low or low == "po":
        return "Purchase Order"
    if low == "quote":
        return "Quote"
    if low == "rfq" or "request for quote" in low:
        return "RFQ"
    if "send information" in low or low in {"send e-mail", "send email", "fyi"}:
        return "Send Information"
    if "weblead" in low or "web lead" in low:
        return "WebLead"
    if low in {"hot", "hot prospect"}:
        return "Hot"
    if t in _ENGAGEMENT_SIGNAL_RANK:
        return t
    return ""


def _build_engagement_assessment(
    *,
    known: ResearchNorthStarKnown,
    client_name: str,
) -> ResearchEngagementAssessment:
    """
    Opportunity / Engagement assessment from stored NorthStar commercial signals
    and imported client sales_events. Never converts engagement into Campaign Fit.
    """
    client = _blank(client_name) or _blank(known.working_for_client_name) or "this client"
    own_signals: set[str] = set()

    for m in known.milestones or []:
        if not isinstance(m, dict):
            continue
        m_client = _blank(m.get("client_name"))
        if m_client and m_client != client:
            continue
        sig = _normalize_engagement_signal(
            _blank(m.get("milestone_type")) or _blank(m.get("label"))
        )
        if sig:
            own_signals.add(sig)

    # Status aliases that are themselves engagement milestones
    status_sig = _normalize_engagement_signal(known.working_for_status)
    if status_sig:
        own_signals.add(status_sig)

    # Imported sales events for Working For client only
    se_level_hint = ""
    try:
        from sales_events_intel import sales_event_engagement_signals

        se_rows = [
            e
            for e in (known.sales_events or [])
            if isinstance(e, dict)
            and (
                not _blank(e.get("client_name"))
                or _blank(e.get("client_name")) == client
                or int(e.get("client_id") or 0) == int(known.working_for_client_id or 0)
            )
        ]
        se_signals, se_level_hint = sales_event_engagement_signals(
            se_rows, closed_status=known.working_for_status
        )
        for sig in se_signals:
            own_signals.add(sig)
    except Exception:
        se_level_hint = ""

    for row in known.cross_client or []:
        if not isinstance(row, dict):
            continue
        if _blank(row.get("client_name")) != client:
            continue
        for s in row.get("signals") or []:
            sig = _normalize_engagement_signal(str(s))
            if sig:
                own_signals.add(sig)

    ranked = sorted(
        own_signals,
        key=lambda s: (-_ENGAGEMENT_SIGNAL_RANK.get(s, 0), s),
    )

    cross_rows: list[dict] = []
    for row in known.cross_client or []:
        if not isinstance(row, dict):
            continue
        cname = _blank(row.get("client_name"))
        if not cname or cname == client:
            continue
        sigs = []
        for s in row.get("signals") or []:
            sig = _normalize_engagement_signal(str(s))
            if sig and sig not in sigs:
                sigs.append(sig)
        # Also surface other-client sales events as labeled history, not own engagement
        for s in row.get("sales_event_signals") or []:
            sig = _normalize_engagement_signal(str(s))
            if sig and sig not in sigs:
                sigs.append(sig)
        if sigs:
            cross_rows.append(
                {
                    "client_name": cname,
                    "signals": sigs,
                    "attribution": _blank(row.get("attribution"))
                    or f"{cname} History — not {client} Campaign Fit or owned engagement.",
                }
            )

    status_l = _blank(known.working_for_status).lower()
    closed = any(x in status_l for x in ("closed", "disqual", "do not call", "dnc", "lost"))

    if not ranked and not cross_rows:
        return ResearchEngagementAssessment(
            level="Insufficient Information",
            label="No stored engagement signals",
            signals=[],
            attributed_to=client,
            why=(
                f"No Purchase Order, Quote, RFQ, Appointment, WebLead, Hot, or "
                f"Send Information signals are on file for {client}."
            ),
        )

    if closed:
        level = "Closed / Not Pursuing"
        label = known.working_for_status or "Closed"
    elif se_level_hint in {
        "Active Opportunity",
        "Engaged",
        "Early Engagement",
        "Closed / Not Pursuing",
    }:
        level = se_level_hint
        label = ranked[0] if ranked else se_level_hint
    elif "Purchase Order" in ranked:
        level = "Proven Commercial Engagement"
        label = "Purchase Order on file"
    elif "Quote" in ranked:
        level = "Active Commercial Engagement"
        label = "Quote on file"
    elif "RFQ" in ranked:
        level = "Active Opportunity"
        label = "RFQ on file"
    elif "Appointment Set" in ranked:
        level = "Engaged"
        label = "Appointment history"
    elif "Send Information" in ranked:
        level = "Early Engagement"
        label = "Send Information"
    elif ranked:
        level = "Early Engagement"
        label = ranked[0]
    else:
        level = "Cross-Client Engagement"
        label = "Other-client engagement only"

    if ranked:
        why = (
            f"{client} has stored engagement signal(s): {', '.join(ranked)}. "
            "This increases confidence to pursue the account and does not change "
            "Campaign Fit by itself. Historical RFQs are not treated as currently "
            "active without supporting context."
        )
    else:
        why = (
            "Engagement signals exist for other authorized clients only. "
            f"They inform prioritization for {client} but are not {client} Campaign Fit."
        )

    return ResearchEngagementAssessment(
        level=level,
        label=label,
        signals=ranked,
        attributed_to=client,
        why=why,
        cross_client_signals=cross_rows,
    )


def start_company_research(
    body: ResearchStartRequest,
    *,
    user_id: int | None = None,
) -> ResearchCompanyResponse:
    ensure_research_schema()
    from client_setup_data import (
        campaign_to_fit_profile,
        ensure_client_setup_schema,
        get_campaign_for_client,
    )

    ensure_client_setup_schema()
    user = get_default_user() if user_id is None else None
    if user_id is not None:
        from access import get_user_by_id

        user = get_user_by_id(user_id)
    if user is None:
        raise PermissionError("User not found.")

    visible_ids = resolve_visibility_client_ids(user.id)
    with get_connection() as conn:
        _seed_client_target_profiles(conn)
        company = _resolve_company(
            conn,
            company_id=body.company_id,
            external_record_no=body.external_record_no,
        )
        company_id = int(company["id"])
        rels = _authorized_relationships(
            conn, company_id=company_id, visible_ids=visible_ids
        )

        working_id = body.working_for_client_id
        if working_id is not None and working_id <= 0:
            working_id = None

        if working_id is None:
            # Require explicit client when multiple authorized relationships exist
            # or when none exist (still need Working For for fit).
            choices = [
                {
                    "client_id": int(r["client_id"]),
                    "client_name": _blank(r["client_name"]),
                    "external_record_no": _blank(r["external_record_no"]),
                    "status": _blank(r["status"]),
                }
                for r in rels
            ]
            # Also offer all authorized clients even without CCR (soft-open)
            if not choices:
                for cid in visible_ids:
                    row = conn.execute(
                        "SELECT id, name FROM clients WHERE id = ?", (cid,)
                    ).fetchone()
                    if row:
                        choices.append(
                            {
                                "client_id": int(row["id"]),
                                "client_name": _blank(row["name"]),
                                "external_record_no": "",
                                "status": "",
                            }
                        )
            elif len(choices) == 1:
                working_id = int(choices[0]["client_id"])
            else:
                # Multiple — never guess
                return ResearchCompanyResponse(
                    company_id=company_id,
                    company_name=_blank(company["company_name"]),
                    needs_working_for=True,
                    working_for_choices=choices,
                    summary=(
                        "Which client are you researching this company for? "
                        "Research evaluates fit for one Working For client."
                    ),
                    data_provider={
                        "status": "ZoomInfo not connected",
                        "future_capabilities": [
                            "Company firmographics",
                            "Employee count",
                            "Revenue",
                            "Decision makers",
                            "Titles",
                            "Business emails",
                            "Direct phones",
                            "Mobile phones",
                        ],
                    },
                )

        if working_id not in visible_ids and not user_can_access_client(
            user.id, working_id
        ):
            raise PermissionError("Not authorized for that Working For client.")

        known = _gather_northstar_known(
            conn,
            user_id=user.id,
            company=company,
            working_for_client_id=working_id,
            visible_ids=visible_ids,
            campaign_id=body.campaign_id,
        )

        # Freshness: reuse recent run unless force_refresh
        if not body.force_refresh:
            recent = conn.execute(
                """
                SELECT id FROM company_research_runs
                WHERE company_id = ? AND working_for_client_id = ?
                  AND completed_at IS NOT NULL
                  AND completed_at >= datetime('now', '-7 days')
                ORDER BY completed_at DESC
                LIMIT 1
                """,
                (company_id, working_id),
            ).fetchone()
            if recent:
                return get_research_run(int(recent["id"]), user_id=user.id)

        # External research — minimum context only (include campaign personas)
        personas, persona_source, preloaded_campaign = _resolve_research_personas(
            conn,
            working_id=working_id,
            campaign_id=body.campaign_id,
        )
        context = ResearchContext(
            company_name=_blank(company["company_name"]),
            website=_blank(company["website"]),
            city=_blank(company["city"]),
            state=_blank(company["state"]),
            working_for_client_name=known.working_for_client_name,
            target_personas=personas,
            persona_source=persona_source,
        )
        providers = get_research_providers()
        provider_results = [p.research_company(context) for p in providers]

        public = next(
            (r for r in provider_results if r.provider_id == "public_web"), None
        )
        zoom = next(
            (r for r in provider_results if r.provider_id == "zoominfo"), None
        )
        findings = list(public.findings) if public else []
        sources_checked = list(public.sources_checked) if public else []
        providers_used = [
            r.provider_name
            for r in provider_results
            if r.status == "ok" or r.provider_id == "zoominfo"
        ]

        cur = conn.execute(
            """
            INSERT INTO company_research_runs (
                company_id, working_for_client_id, initiated_by_user_id,
                initiated_by_name, status, summary, providers_used, sources_checked,
                started_at, completed_at, campaign_id
            ) VALUES (?, ?, ?, ?, 'completed', '', ?, ?, ?, ?, ?)
            """,
            (
                company_id,
                working_id,
                user.id,
                _blank(user.full_name) or _blank(user.email),
                ", ".join(providers_used),
                json.dumps(sources_checked),
                _now(),
                _now(),
                body.campaign_id,
            ),
        )
        run_id = int(cur.lastrowid)

        verified_caps: list[str] = []
        verified_inds: list[str] = []
        verified_products: list[str] = []
        research_text_blobs: list[str] = []
        research_website = ""
        finding_rows: list[ResearchFindingView] = []
        pages_researched = [
            {"url": p.url, "title": p.title, "label": p.label}
            for p in ((public.pages_researched if public else []) or [])
        ]

        for f in findings:
            evidence_level = getattr(f, "evidence_level", "verified") or "verified"
            page_title = getattr(f, "page_title", "") or ""
            try:
                conn.execute(
                    """
                    INSERT INTO company_research_findings (
                        research_run_id, company_id, finding_type, field_key, value,
                        source_name, source_url, researched_at, confidence,
                        is_public_contact, contact_name, contact_title, provider_id,
                        evidence_level, page_title
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        company_id,
                        f.finding_type,
                        f.field_key,
                        f.value,
                        f.source_name,
                        f.source_url,
                        f.researched_at,
                        f.confidence,
                        1 if f.is_public_contact else 0,
                        f.contact_name,
                        f.contact_title,
                        f.provider_id,
                        evidence_level,
                        page_title,
                    ),
                )
            except Exception:
                conn.execute(
                    """
                    INSERT INTO company_research_findings (
                        research_run_id, company_id, finding_type, field_key, value,
                        source_name, source_url, researched_at, confidence,
                        is_public_contact, contact_name, contact_title, provider_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        company_id,
                        f.finding_type,
                        f.field_key,
                        f.value,
                        f.source_name,
                        f.source_url,
                        f.researched_at,
                        f.confidence,
                        1 if f.is_public_contact else 0,
                        f.contact_name,
                        f.contact_title,
                        f.provider_id,
                    ),
                )
            finding_rows.append(
                ResearchFindingView(
                    finding_type=f.finding_type,
                    field_key=f.field_key,
                    value=f.value,
                    source_name=f.source_name,
                    source_url=f.source_url,
                    researched_at=f.researched_at,
                    confidence=f.confidence,
                    evidence_level=evidence_level,
                    page_title=page_title,
                    is_public_contact=f.is_public_contact,
                    contact_name=f.contact_name,
                    contact_title=f.contact_title,
                    provider_id=f.provider_id,
                )
            )
            if f.finding_type == "people_discovery":
                continue
            if f.value and f.value != NOT_VERIFIED and evidence_level == "verified":
                if f.finding_type == "capability":
                    verified_caps.append(f.value)
                elif f.finding_type == "industry":
                    verified_inds.append(f.value)
                elif f.finding_type == "product":
                    verified_products.append(f.value)
                elif f.finding_type == "website":
                    research_website = f.value
                elif f.finding_type in {
                    "material",
                    "overview",
                    "process",
                    "capability_detail",
                    "manufacturing",
                }:
                    research_text_blobs.append(f.value)
                # Shared master intelligence (verified facts only)
                if f.field_key and f.value != NOT_VERIFIED and "not explicitly" not in f.value.lower():
                    conn.execute(
                        """
                        INSERT INTO company_intelligence (
                            company_id, field_key, value, finding_type, source_name,
                            source_url, confidence, last_verified_at, research_run_id,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(company_id, field_key) DO UPDATE SET
                            value = excluded.value,
                            finding_type = excluded.finding_type,
                            source_name = excluded.source_name,
                            source_url = excluded.source_url,
                            confidence = excluded.confidence,
                            last_verified_at = excluded.last_verified_at,
                            research_run_id = excluded.research_run_id,
                            updated_at = excluded.updated_at
                        """,
                        (
                            company_id,
                            f.field_key if f.finding_type != "product" else f"product:{f.value}",
                            f.value,
                            f.finding_type,
                            f.source_name,
                            f.source_url,
                            f.confidence,
                            f.researched_at,
                            run_id,
                            _now(),
                            _now(),
                        ),
                    )

        # Headquarters confirmation when city appears on researched pages
        ns_city = _blank(company["city"])
        ns_state = _blank(company["state"])
        if ns_city and any(
            ns_city.lower() in f.value.lower()
            for f in findings
            if f.value and f.value != NOT_VERIFIED
        ):
            hq = f"{ns_city}, {ns_state}".strip(", ")
            if not any(
                x.finding_type == "headquarters" and x.value != NOT_VERIFIED
                for x in finding_rows
            ):
                finding_rows.append(
                    ResearchFindingView(
                        finding_type="headquarters",
                        field_key="headquarters",
                        value=hq,
                        source_name="NorthStar master + official website confirmation",
                        source_url=research_website or "",
                        researched_at=_now(),
                        confidence="medium",
                        evidence_level="verified",
                        page_title="",
                    )
                )

        verified_matches: list[ResearchVerificationItem] = []
        possible_changes: list[ResearchVerificationItem] = []
        proposed_views: list[ResearchProposedUpdateView] = []

        # Website verification / proposed update
        ns_website = _blank(company["website"])
        if research_website and research_website != NOT_VERIFIED:
            if _values_equivalent(ns_website, research_website, "website"):
                verified_matches.append(
                    ResearchVerificationItem(
                        field_key="website",
                        label="Website",
                        northstar_value=ns_website,
                        research_value=research_website,
                        source_name="Official company website",
                        source_url=research_website,
                        match=True,
                    )
                )
            elif ns_website:
                # Proposed change — do not write master
                cur_u = conn.execute(
                    """
                    INSERT INTO research_proposed_updates (
                        company_id, research_run_id, field_key, current_value,
                        proposed_value, source_name, source_url, research_date,
                        confidence, status
                    ) VALUES (?, ?, 'website', ?, ?, ?, ?, ?, 'high', 'Pending Review')
                    """,
                    (
                        company_id,
                        run_id,
                        ns_website,
                        research_website,
                        "Official company website",
                        research_website,
                        _now(),
                    ),
                )
                pid = int(cur_u.lastrowid)
                item = ResearchVerificationItem(
                    field_key="website",
                    label="Website",
                    northstar_value=ns_website,
                    research_value=research_website,
                    source_name="Official company website",
                    source_url=research_website,
                    match=False,
                    proposed_update_id=pid,
                )
                possible_changes.append(item)
                proposed_views.append(
                    ResearchProposedUpdateView(
                        id=pid,
                        company_id=company_id,
                        field_key="website",
                        current_value=ns_website,
                        proposed_value=research_website,
                        source_name="Official company website",
                        source_url=research_website,
                        research_date=_now(),
                        confidence="high",
                        status="Pending Review",
                    )
                )
            else:
                # Empty NorthStar website → propose add
                cur_u = conn.execute(
                    """
                    INSERT INTO research_proposed_updates (
                        company_id, research_run_id, field_key, current_value,
                        proposed_value, source_name, source_url, research_date,
                        confidence, status
                    ) VALUES (?, ?, 'website', '', ?, ?, ?, ?, 'high', 'Pending Review')
                    """,
                    (
                        company_id,
                        run_id,
                        research_website,
                        "Official company website",
                        research_website,
                        _now(),
                    ),
                )
                pid = int(cur_u.lastrowid)
                possible_changes.append(
                    ResearchVerificationItem(
                        field_key="website",
                        label="Website",
                        northstar_value="",
                        research_value=research_website,
                        source_name="Official company website",
                        source_url=research_website,
                        match=False,
                        proposed_update_id=pid,
                    )
                )
                proposed_views.append(
                    ResearchProposedUpdateView(
                        id=pid,
                        company_id=company_id,
                        field_key="website",
                        current_value="",
                        proposed_value=research_website,
                        source_name="Official company website",
                        source_url=research_website,
                        research_date=_now(),
                        confidence="high",
                        status="Pending Review",
                    )
                )

        # City/state verification (informational — no auto propose unless research differs clearly)
        if ns_city and ns_state:
            verified_matches.append(
                ResearchVerificationItem(
                    field_key="headquarters",
                    label="Headquarters (NorthStar)",
                    northstar_value=f"{ns_city}, {ns_state}",
                    research_value=(
                        f"{ns_city}, {ns_state}"
                        if any(
                            f.finding_type == "headquarters" for f in finding_rows
                        )
                        else NOT_VERIFIED
                    ),
                    source_name="NorthStar master record",
                    source_url="",
                    match=True,
                )
            )

        # Resolve campaign (default when not specified) — source of truth for Fit
        campaign = preloaded_campaign or get_campaign_for_client(
            conn, working_id, body.campaign_id
        )
        campaign_id = int(campaign["id"]) if campaign else None
        campaign_name = _blank((campaign or {}).get("campaign_name"))
        if campaign:
            # Persist resolved campaign on the run when column exists
            try:
                conn.execute(
                    "UPDATE company_research_runs SET campaign_id = ? WHERE id = ?",
                    (campaign_id, run_id),
                )
            except Exception:
                pass
            profile = campaign_to_fit_profile(campaign)
        else:
            profile_row = conn.execute(
                "SELECT * FROM client_target_profiles WHERE client_id = ?",
                (working_id,),
            ).fetchone()
            profile = dict(profile_row) if profile_row else None
        fit = _compute_fit(
            client_name=known.working_for_client_name,
            client_id=working_id,
            profile=profile,
            known=known,
            verified_caps=verified_caps,
            verified_products=verified_products,
            verified_inds=verified_inds,
            campaign_id=campaign_id,
            campaign_name=campaign_name,
            research_text_blobs=research_text_blobs,
        )
        conn.execute(
            """
            INSERT INTO company_client_fit (
                company_id, client_id, research_run_id, fit_result, why,
                supporting_evidence, potential_opportunity, concerns,
                missing_information, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company_id, client_id) DO UPDATE SET
                research_run_id = excluded.research_run_id,
                fit_result = excluded.fit_result,
                why = excluded.why,
                supporting_evidence = excluded.supporting_evidence,
                potential_opportunity = excluded.potential_opportunity,
                concerns = excluded.concerns,
                missing_information = excluded.missing_information,
                updated_at = excluded.updated_at
            """,
            (
                company_id,
                working_id,
                run_id,
                fit.fit_result,
                fit.why,
                json.dumps(
                    {
                        "supporting_evidence": fit.supporting_evidence,
                        "evidence_chains": fit.evidence_chains,
                        "profile_fields_used": fit.profile_fields_used,
                        "profile_incomplete": fit.profile_incomplete,
                        "profile_gaps": fit.profile_gaps,
                    }
                ),
                fit.potential_opportunity,
                json.dumps(fit.concerns),
                json.dumps(fit.missing_information),
                _now(),
                _now(),
            ),
        )

        summary = _build_summary(
            company_name=_blank(company["company_name"]),
            known=known,
            fit=fit,
            verified_products=verified_products,
            verified_caps=verified_caps,
            pages_count=len(pages_researched),
            verified_inds=verified_inds,
            overview_bits=[
                f.value
                for f in finding_rows
                if f.finding_type == "overview"
                and f.value
                and f.value != NOT_VERIFIED
                and (f.evidence_level or "verified") != "not_verified"
            ],
            website=research_website or _blank(company["website"]),
            locations=list(
                dict.fromkeys(
                    [
                        f.value
                        for f in finding_rows
                        if f.finding_type in {"headquarters", "location"}
                        and f.value
                        and f.value != NOT_VERIFIED
                        and (f.evidence_level or "verified") != "not_verified"
                    ]
                )
            ),
            stamping_primary="stamp"
            in _blank((profile or {}).get("primary_service")).lower(),
        )
        conn.execute(
            "UPDATE company_research_runs SET summary = ?, completed_at = ? WHERE id = ?",
            (summary, _now(), run_id),
        )
        conn.commit()

        return _assemble_response(
            conn,
            run_id=run_id,
            company=company,
            known=known,
            finding_rows=finding_rows,
            verified_matches=verified_matches,
            possible_changes=possible_changes,
            proposed_views=proposed_views,
            fit=fit,
            summary=summary,
            sources_checked=sources_checked,
            pages_researched=pages_researched,
            zoom=zoom,
            initiated_by=_blank(user.full_name) or _blank(user.email),
            last_researched_at=_now(),
        )


def _assemble_response(
    conn,
    *,
    run_id: int,
    company: dict[str, Any],
    known: ResearchNorthStarKnown,
    finding_rows: list[ResearchFindingView],
    verified_matches: list[ResearchVerificationItem],
    possible_changes: list[ResearchVerificationItem],
    proposed_views: list[ResearchProposedUpdateView],
    fit: ResearchFitView,
    summary: str,
    sources_checked: list[str],
    zoom,
    initiated_by: str,
    last_researched_at: str,
    pages_researched: list[dict] | None = None,
) -> ResearchCompanyResponse:
    def bucket(types: set[str]) -> list[ResearchFindingView]:
        return [f for f in finding_rows if f.finding_type in types]

    sources = []
    seen_src: set[str] = set()
    for f in finding_rows:
        if f.finding_type == "people_discovery":
            continue
        key = f"{f.source_name}|{f.source_url}|{f.page_title}"
        if key in seen_src:
            continue
        seen_src.add(key)
        if f.source_name or f.source_url:
            sources.append(
                {
                    "source_name": f.source_name,
                    "source_url": f.source_url,
                    "page_title": f.page_title,
                    "researched_at": f.researched_at,
                    "finding_type": f.finding_type,
                    "evidence_level": f.evidence_level,
                }
            )
    for url in sources_checked:
        if url and url not in seen_src:
            sources.append(
                {
                    "source_name": "Source checked",
                    "source_url": url,
                    "page_title": "",
                    "researched_at": last_researched_at,
                    "finding_type": "search",
                    "evidence_level": "verified",
                }
            )

    history = [
        {
            "research_run_id": int(r["id"]),
            "completed_at": _blank(r["completed_at"] or r["started_at"]),
            "initiated_by": _blank(r["initiated_by_name"]),
            "summary": _blank(r["summary"])[:240],
            "providers_used": _blank(r["providers_used"]),
        }
        for r in conn.execute(
            """
            SELECT id, completed_at, started_at, initiated_by_name, summary, providers_used
            FROM company_research_runs
            WHERE company_id = ?
            ORDER BY COALESCE(completed_at, started_at) DESC
            LIMIT 10
            """,
            (int(company["id"]),),
        ).fetchall()
    ]

    missing = list(fit.missing_information)
    for f in finding_rows:
        if (
            (f.value == NOT_VERIFIED or f.evidence_level == "not_verified")
            and f.finding_type
            not in {
                "development",
                "public_contact",
                "people_discovery",
            }
        ):
            label = f.finding_type.replace("_", " ")
            msg = f"{label.title()}: {NOT_VERIFIED}"
            if msg not in missing:
                missing.append(msg)

    campaign_choices: list[dict] = []
    campaign_id = fit.campaign_id
    campaign_name = fit.campaign_name
    working_id = known.working_for_client_id
    if working_id:
        try:
            for cr in conn.execute(
                """
                SELECT id, campaign_name, is_default, is_active
                FROM client_campaigns
                WHERE client_id = ? AND is_active = 1
                ORDER BY is_default DESC, campaign_name COLLATE NOCASE
                """,
                (working_id,),
            ).fetchall():
                campaign_choices.append(
                    {
                        "campaign_id": int(cr["id"]),
                        "campaign_name": _blank(cr["campaign_name"]),
                        "is_default": bool(cr["is_default"]),
                    }
                )
            if not campaign_id and campaign_choices:
                default = next(
                    (c for c in campaign_choices if c["is_default"]),
                    campaign_choices[0],
                )
                campaign_id = int(default["campaign_id"])
                campaign_name = _blank(default["campaign_name"])
        except Exception:
            pass

    decision = _build_decision_summary(
        company_name=_blank(company["company_name"]),
        client_name=known.working_for_client_name,
        campaign_name=campaign_name or _blank(fit.campaign_name),
        fit=fit,
        verified_products=[
            f.value
            for f in finding_rows
            if f.finding_type == "product"
            and f.value
            and f.value != NOT_VERIFIED
            and (f.evidence_level or "verified") != "not_verified"
        ],
        verified_caps=[
            f.value
            for f in finding_rows
            if f.finding_type == "capability"
            and f.value
            and f.value != NOT_VERIFIED
            and (f.evidence_level or "verified") != "not_verified"
        ],
        overview_bits=[
            f.value
            for f in finding_rows
            if f.finding_type in {"overview", "company_name"}
            and f.value
            and f.value != NOT_VERIFIED
        ],
    )
    engagement = _build_engagement_assessment(
        known=known,
        client_name=known.working_for_client_name,
    )
    from recommendation_data import build_northstar_recommendation

    recommendation = build_northstar_recommendation(
        fit_result=_blank(fit.fit_result) if fit else "",
        engagement_level=engagement.level if engagement else "",
        engagement_signals=list(engagement.signals) if engagement else [],
        relationship_status=known.working_for_status,
        sales_events=list(known.sales_events or []),
        contacts=list(known.contacts or []),
        missing_information=list(missing or []),
        research_gaps=list(fit.missing_information) if fit and fit.missing_information else [],
        campaign_name=campaign_name,
        client_name=known.working_for_client_name,
    )

    # Prospecting Guidance — targeting context only; never feeds Campaign Fit
    targeting = None
    if known.working_for_client_id:
        try:
            from client_knowledge_data import get_stored_knowledge_field
            from models import ResearchTargetingGuidance

            pg = get_stored_knowledge_field(
                int(known.working_for_client_id),
                "strategy",
                "prospecting_guidance",
            )
            if pg:
                targeting = ResearchTargetingGuidance(prospecting_guidance=pg)
        except Exception:
            targeting = None

    return ResearchCompanyResponse(
        research_run_id=run_id,
        company_id=int(company["id"]),
        company_name=_blank(company["company_name"]),
        working_for_client_id=known.working_for_client_id,
        working_for_client_name=known.working_for_client_name,
        working_for_record_no=known.working_for_record_no,
        working_for_status=known.working_for_status,
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        campaign_choices=campaign_choices,
        summary=summary,
        decision_summary=decision,
        engagement=engagement,
        recommendation=recommendation,
        last_researched_at=last_researched_at,
        initiated_by=initiated_by,
        northstar_known=known,
        verified_matches=verified_matches,
        possible_changes=possible_changes,
        overview=bucket({"overview", "company_name", "website", "phone"}),
        locations=bucket({"headquarters", "location"}),
        capabilities=bucket({"capability"}),
        products=bucket({"product"}),
        materials=bucket({"material"}),
        industries=bucket({"industry"}),
        recent_developments=bucket({"development"}),
        missing_information=missing,
        northstar_contacts=known.contacts,
        northstar_contacts_total=int(known.contacts_total or 0),
        public_contacts=_public_contact_views(conn, int(company["id"]), finding_rows),
        people_discovery=_extract_people_discovery(finding_rows),
        cross_client_experience=known.cross_client,
        fit=fit,
        targeting_guidance=targeting,
        proposed_updates=proposed_views,
        sources=sources,
        pages_researched=pages_researched or [],
        data_provider={
            "status": (zoom.status_detail if zoom else "ZoomInfo not connected"),
            "future_capabilities": (
                zoom.future_capabilities
                if zoom
                else [
                    "Company firmographics",
                    "Employee count",
                    "Revenue",
                    "Decision makers",
                    "Titles",
                    "Business emails",
                    "Direct phones",
                    "Mobile phones",
                ]
            ),
        },
        research_history=history,
        read_only_crm=True,
    )


def get_research_run(run_id: int, *, user_id: int | None = None) -> ResearchCompanyResponse:
    ensure_research_schema()
    user = get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    visible_ids = resolve_visibility_client_ids(user.id)
    with get_connection() as conn:
        run = conn.execute(
            "SELECT * FROM company_research_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not run:
            raise LookupError("Research run not found.")
        company = _resolve_company(conn, company_id=int(run["company_id"]), external_record_no=None)
        working_id = int(run["working_for_client_id"])
        if working_id not in visible_ids:
            raise PermissionError("Not authorized for this research run.")
        known = _gather_northstar_known(
            conn,
            user_id=user.id,
            company=company,
            working_for_client_id=working_id,
            visible_ids=visible_ids,
            campaign_id=int(run["campaign_id"]) if run["campaign_id"] else None,
        )
        findings = []
        for r in conn.execute(
            """
            SELECT * FROM company_research_findings
            WHERE research_run_id = ?
            ORDER BY id
            """,
            (run_id,),
        ).fetchall():
            row = dict(r)
            findings.append(
                ResearchFindingView(
                    id=int(row["id"]),
                    finding_type=_blank(row["finding_type"]),
                    field_key=_blank(row["field_key"]),
                    value=_blank(row["value"]),
                    source_name=_blank(row["source_name"]),
                    source_url=_blank(row["source_url"]),
                    researched_at=_blank(row["researched_at"]),
                    confidence=_blank(row["confidence"]),
                    evidence_level=_blank(row.get("evidence_level")) or "verified",
                    page_title=_blank(row.get("page_title")),
                    is_public_contact=bool(row["is_public_contact"]),
                    contact_name=_blank(row["contact_name"]),
                    contact_title=_blank(row["contact_title"]),
                    provider_id=_blank(row["provider_id"]),
                )
            )
        proposed = [
            ResearchProposedUpdateView(
                id=int(r["id"]),
                company_id=int(r["company_id"]),
                field_key=_blank(r["field_key"]),
                current_value=_blank(r["current_value"]),
                proposed_value=_blank(r["proposed_value"]),
                source_name=_blank(r["source_name"]),
                source_url=_blank(r["source_url"]),
                research_date=_blank(r["research_date"]),
                confidence=_blank(r["confidence"]),
                status=_blank(r["status"]),
            )
            for r in conn.execute(
                """
                SELECT * FROM research_proposed_updates
                WHERE research_run_id = ?
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        ]
        fit_row = conn.execute(
            """
            SELECT * FROM company_client_fit
            WHERE company_id = ? AND client_id = ?
            """,
            (int(company["id"]), working_id),
        ).fetchone()
        support_raw: Any = []
        chains: list[dict] = []
        profile_fields: list[str] = []
        profile_gaps: list[str] = []
        profile_incomplete = False
        if fit_row:
            try:
                parsed = json.loads(fit_row["supporting_evidence"] or "[]")
            except json.JSONDecodeError:
                parsed = []
            if isinstance(parsed, dict):
                support_raw = parsed.get("supporting_evidence") or []
                chains = parsed.get("evidence_chains") or []
                profile_fields = parsed.get("profile_fields_used") or []
                profile_gaps = parsed.get("profile_gaps") or []
                profile_incomplete = bool(parsed.get("profile_incomplete"))
            else:
                support_raw = parsed
        run_d = dict(run)
        fit = ResearchFitView(
            client_id=working_id,
            client_name=known.working_for_client_name,
            campaign_id=int(run_d["campaign_id"])
            if run_d.get("campaign_id") is not None
            else None,
            campaign_name="",
            fit_result=_blank(fit_row["fit_result"]) if fit_row else "Insufficient Information",
            why=_blank(fit_row["why"]) if fit_row else "",
            supporting_evidence=list(support_raw) if isinstance(support_raw, list) else [],
            evidence_chains=chains,
            potential_opportunity=_blank(fit_row["potential_opportunity"]) if fit_row else "",
            concerns=json.loads(fit_row["concerns"] or "[]") if fit_row else [],
            missing_information=json.loads(fit_row["missing_information"] or "[]")
            if fit_row
            else [],
            profile_fields_used=profile_fields,
            profile_incomplete=profile_incomplete,
            profile_gaps=profile_gaps,
        )
        # Resolve campaign name for display
        if fit.campaign_id:
            crow = conn.execute(
                "SELECT campaign_name FROM client_campaigns WHERE id = ?",
                (fit.campaign_id,),
            ).fetchone()
            if crow:
                fit.campaign_name = _blank(crow["campaign_name"])
        elif working_id:
            from client_setup_data import get_default_campaign

            dc = get_default_campaign(conn, working_id)
            if dc:
                fit.campaign_id = int(dc["id"])
                fit.campaign_name = _blank(dc["campaign_name"])
        verified: list[ResearchVerificationItem] = []
        possible: list[ResearchVerificationItem] = []
        for p in proposed:
            item = ResearchVerificationItem(
                field_key=p.field_key,
                label=p.field_key.replace("_", " ").title(),
                northstar_value=p.current_value,
                research_value=p.proposed_value,
                source_name=p.source_name,
                source_url=p.source_url,
                match=False,
                proposed_update_id=p.id if p.status == "Pending Review" else None,
            )
            if p.status == "Pending Review":
                possible.append(item)
            elif p.status == "Approved":
                verified.append(
                    ResearchVerificationItem(
                        **{**item.model_dump(), "match": True, "proposed_update_id": None}
                    )
                )
        sources_checked = []
        try:
            sources_checked = json.loads(_blank(run["sources_checked"]) or "[]")
        except json.JSONDecodeError:
            sources_checked = []
        return _assemble_response(
            conn,
            run_id=run_id,
            company=company,
            known=known,
            finding_rows=findings,
            verified_matches=verified,
            possible_changes=possible,
            proposed_views=proposed,
            fit=fit,
            summary=_blank(run["summary"]),
            sources_checked=sources_checked,
            pages_researched=[],
            zoom=None,
            initiated_by=_blank(run["initiated_by_name"]),
            last_researched_at=_blank(run["completed_at"] or run["started_at"]),
        )


def get_latest_research(
    *,
    company_id: int | None = None,
    external_record_no: str | None = None,
    working_for_client_id: int | None = None,
) -> ResearchCompanyResponse:
    """Return latest stored research or start clarification response."""
    ensure_research_schema()
    user = get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    with get_connection() as conn:
        company = _resolve_company(
            conn, company_id=company_id, external_record_no=external_record_no
        )
        q = """
            SELECT id FROM company_research_runs
            WHERE company_id = ?
        """
        params: list[Any] = [int(company["id"])]
        if working_for_client_id:
            q += " AND working_for_client_id = ?"
            params.append(working_for_client_id)
        q += " ORDER BY COALESCE(completed_at, started_at) DESC LIMIT 1"
        row = conn.execute(q, params).fetchone()
        if row:
            return get_research_run(int(row["id"]))
    # No prior run — start research (may ask for Working For)
    return start_company_research(
        ResearchStartRequest(
            company_id=int(company["id"]),
            working_for_client_id=working_for_client_id,
            force_refresh=False,
        )
    )


def approve_proposed_update(
    body: ResearchApproveRequest, *, user_id: int | None = None
) -> dict[str, Any]:
    ensure_research_schema()
    user = get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    approved_by = _blank(body.approved_by) or _blank(user.full_name) or "NorthStar User"

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM research_proposed_updates WHERE id = ?",
            (body.proposed_update_id,),
        ).fetchone()
        if not row:
            raise LookupError("Proposed update not found.")
        if _blank(row["status"]) != "Pending Review":
            raise ValueError(f"Update is already {_blank(row['status'])}.")

        field_key = _blank(row["field_key"])
        column = APPROVABLE_MASTER_FIELDS.get(field_key)
        if not column:
            raise ValueError(f"Field “{field_key}” is not approvable on the master company.")

        company_id = int(row["company_id"])
        company = conn.execute(
            f"SELECT id, external_record_no, {column} AS current_val FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        if not company:
            raise LookupError("Company not found.")

        old_value = _blank(company["current_val"])
        new_value = _blank(row["proposed_value"])
        # Apply ONLY the intended master field
        conn.execute(
            f"UPDATE companies SET {column} = ?, last_updated_at = ? WHERE id = ?",
            (new_value, _now(), company_id),
        )
        conn.execute(
            """
            UPDATE research_proposed_updates
            SET status = 'Approved', reviewed_by = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (approved_by, _now(), body.proposed_update_id),
        )
        conn.execute(
            """
            INSERT INTO research_update_audit (
                proposed_update_id, company_id, field_key, old_value, new_value,
                source_name, source_url, approved_by, approved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                body.proposed_update_id,
                company_id,
                field_key,
                old_value,
                new_value,
                _blank(row["source_name"]),
                _blank(row["source_url"]),
                approved_by,
                _now(),
            ),
        )
        # Also log in field_audit_log for workspace consistency
        conn.execute(
            """
            INSERT INTO field_audit_log (
                client_code, external_record_no, field_name,
                old_value, new_value, changed_by, changed_at
            ) VALUES ('master', ?, ?, ?, ?, ?, ?)
            """,
            (
                _blank(company["external_record_no"]),
                f"company.{field_key}",
                old_value,
                new_value,
                approved_by,
                _now(),
            ),
        )
        # Update shared intelligence last_verified
        conn.execute(
            """
            INSERT INTO company_intelligence (
                company_id, field_key, value, finding_type, source_name, source_url,
                confidence, last_verified_at, research_run_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'high', ?, ?, ?, ?)
            ON CONFLICT(company_id, field_key) DO UPDATE SET
                value = excluded.value,
                last_verified_at = excluded.last_verified_at,
                updated_at = excluded.updated_at
            """,
            (
                company_id,
                field_key,
                new_value,
                field_key,
                _blank(row["source_name"]),
                _blank(row["source_url"]),
                _now(),
                row["research_run_id"],
                _now(),
                _now(),
            ),
        )
        conn.commit()
        return {
            "status": "Approved",
            "company_id": company_id,
            "field_key": field_key,
            "old_value": old_value,
            "new_value": new_value,
            "approved_by": approved_by,
            "approved_at": _now(),
        }


def reject_proposed_update(
    body: ResearchRejectRequest, *, user_id: int | None = None
) -> dict[str, Any]:
    ensure_research_schema()
    user = get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    rejected_by = _blank(body.rejected_by) or _blank(user.full_name) or "NorthStar User"
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM research_proposed_updates WHERE id = ?",
            (body.proposed_update_id,),
        ).fetchone()
        if not row:
            raise LookupError("Proposed update not found.")
        if _blank(row["status"]) != "Pending Review":
            raise ValueError(f"Update is already {_blank(row['status'])}.")
        company_id = int(row["company_id"])
        field_key = _blank(row["field_key"])
        # Capture master value before reject to prove unchanged
        column = APPROVABLE_MASTER_FIELDS.get(field_key)
        master_before = ""
        if column:
            crow = conn.execute(
                f"SELECT {column} AS v FROM companies WHERE id = ?", (company_id,)
            ).fetchone()
            master_before = _blank(crow["v"]) if crow else ""
        conn.execute(
            """
            UPDATE research_proposed_updates
            SET status = 'Rejected', reviewed_by = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (rejected_by, _now(), body.proposed_update_id),
        )
        conn.commit()
        master_after = master_before
        if column:
            crow = conn.execute(
                f"SELECT {column} AS v FROM companies WHERE id = ?", (company_id,)
            ).fetchone()
            master_after = _blank(crow["v"]) if crow else ""
        return {
            "status": "Rejected",
            "company_id": company_id,
            "field_key": field_key,
            "master_value_unchanged": master_before == master_after,
            "master_value": master_after,
            "rejected_by": rejected_by,
            "rejected_at": _now(),
        }
