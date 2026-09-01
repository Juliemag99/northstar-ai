"""Client Setup / Target Profile / Campaigns — source of truth for Fit & Ask.

Architecture:
- client_profiles: overview + what the client sells (client-level)
- client_campaigns: multiple ICP / target profiles per client
- Default campaign used when Research/Ask do not name a campaign
- client_target_profiles kept synced from the default campaign (legacy readers)
- Brown stays incomplete — never invent missing fields
- Carmeco approved profile maps to campaign "Stamping" (default)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from access import DEFAULT_USER_EMAIL, get_default_user, get_user_by_id, list_clients_for_user, require_write_client_id, user_can_access_client
from db import get_connection
from models import (
    ClientCampaignUpdate,
    ClientCampaignView,
    ClientListSetupItem,
    ClientOverviewUpdate,
    ClientSetupAuditItem,
    ClientSetupCompleteness,
    ClientSetupResponse,
    ClientSellsUpdate,
)

MANUAL_EDIT_SOURCE = "Manual Client Setup Edit"

EDIT_ROLES = {
    "administrator",
    "admin",
    "management",
    "manager",
    "rev_ops",
    "revops",
    "revenue_ops",
    "operations",
}

# Completeness: Client Information (admin) vs Target Profile (prospect evaluation)
# Weights: higher = more important for Target Profile %. AI Fit uses critical set only.

CLIENT_INFO_FIELDS: list[tuple[str, str, int]] = [
    # (key, label, weight) — source is effective client info dict
    ("client_name", "Client name", 1),
    ("website", "Website", 1),
    ("main_location", "Main location", 1),
    ("main_phone", "Main phone", 1),
    ("description", "Client description", 1),
    ("primary_owner_name", "Primary NorthStar owner", 1),
    ("is_active_set", "Active / Inactive", 1),
]

# Target profile fields for scoring (campaign + sells context)
TARGET_PROFILE_WEIGHTED: list[tuple[str, str, int]] = [
    ("primary_service", "Primary service/capability", 3),
    ("secondary_services", "Secondary capabilities", 2),
    ("target_customer_types", "Ideal customer types", 3),
    ("target_industries", "Target industries", 3),
    ("target_products", "Target products/work", 2),
    ("manufacturing_processes_sought", "Target processes", 3),
    ("production_preference", "Typical outsourced / production preference", 2),
    ("geographic_preferences", "Geography", 2),
    ("positive_signals", "Positive fit signals", 3),
    ("negative_or_exclusions", "Negative fit signals / exclusions", 3),
    ("target_titles", "Target titles", 2),
    ("campaign_defined", "Campaign definition", 2),
    ("fit_weighting_notes", "Fit weighting notes", 2),
]

# Critical for Strong/Possible/Weak Fit decisions — NOT admin fields
AI_FIT_CRITICAL: list[tuple[str, str]] = [
    ("primary_service", "Primary service/capability"),
    ("target_customer_types", "Ideal customer types"),
    ("manufacturing_processes_sought", "Target processes / manufacturing processes sought"),
    ("target_industries", "Target industries"),
    ("positive_signals", "Positive fit signals"),
    ("negative_or_exclusions", "Negative fit signals / exclusions"),
    ("fit_weighting_notes", "Fit weighting notes"),
]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_int(value: object | None, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def ensure_client_setup_schema(conn=None) -> None:
    """Create setup tables and migrate existing target profiles into campaigns."""
    owns = conn is None
    if owns:
        conn = get_connection()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS client_profiles (
                client_id INTEGER PRIMARY KEY,
                website TEXT NOT NULL DEFAULT '',
                main_location TEXT NOT NULL DEFAULT '',
                main_phone TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                primary_owner_name TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                primary_service TEXT NOT NULL DEFAULT '',
                secondary_services TEXT NOT NULL DEFAULT '',
                products_services TEXT NOT NULL DEFAULT '',
                differentiators TEXT NOT NULL DEFAULT '',
                certifications TEXT NOT NULL DEFAULT '',
                equipment_capacity TEXT NOT NULL DEFAULT '',
                value_proposition TEXT NOT NULL DEFAULT '',
                default_campaign_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS client_campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                campaign_name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                is_default INTEGER NOT NULL DEFAULT 0,
                primary_service TEXT NOT NULL DEFAULT '',
                secondary_services TEXT NOT NULL DEFAULT '',
                target_industries TEXT NOT NULL DEFAULT '',
                target_customer_types TEXT NOT NULL DEFAULT '',
                target_products TEXT NOT NULL DEFAULT '',
                manufacturing_processes_sought TEXT NOT NULL DEFAULT '',
                production_preference TEXT NOT NULL DEFAULT '',
                stamping_capability TEXT NOT NULL DEFAULT '',
                tooling_notes TEXT NOT NULL DEFAULT '',
                geographic_preferences TEXT NOT NULL DEFAULT '',
                geography_mode TEXT NOT NULL DEFAULT '',
                geography_required INTEGER NOT NULL DEFAULT 0,
                company_size_preferences TEXT NOT NULL DEFAULT '',
                positive_signals TEXT NOT NULL DEFAULT '',
                negative_signals TEXT NOT NULL DEFAULT '',
                exclusions TEXT NOT NULL DEFAULT '',
                target_titles TEXT NOT NULL DEFAULT '',
                fit_weighting_notes TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                list_source TEXT NOT NULL DEFAULT '',
                import_date TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_client_campaigns_client
                ON client_campaigns(client_id, is_active, is_default);

            CREATE TABLE IF NOT EXISTS client_setup_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                campaign_id INTEGER,
                entity_type TEXT NOT NULL DEFAULT 'client_profile',
                field_name TEXT NOT NULL,
                old_value TEXT NOT NULL DEFAULT '',
                new_value TEXT NOT NULL DEFAULT '',
                changed_by_user_id INTEGER,
                changed_by_name TEXT NOT NULL DEFAULT '',
                changed_at TEXT NOT NULL DEFAULT (datetime('now')),
                change_source TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (campaign_id) REFERENCES client_campaigns(id) ON DELETE SET NULL,
                FOREIGN KEY (changed_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_client_setup_audit_client
                ON client_setup_audit(client_id, changed_at DESC);
            """
        )
        # Additive columns if older DB already had a thinner campaigns table
        camp_cols = {
            str(r["name"])
            for r in conn.execute("PRAGMA table_info(client_campaigns)").fetchall()
        }
        for col, ddl in (
            ("list_source", "TEXT NOT NULL DEFAULT ''"),
            ("import_date", "TEXT NOT NULL DEFAULT ''"),
            ("secondary_services", "TEXT NOT NULL DEFAULT ''"),
            ("stamping_capability", "TEXT NOT NULL DEFAULT ''"),
            ("tooling_notes", "TEXT NOT NULL DEFAULT ''"),
            ("fit_weighting_notes", "TEXT NOT NULL DEFAULT ''"),
            ("geography_mode", "TEXT NOT NULL DEFAULT ''"),
            ("geography_required", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if camp_cols and col not in camp_cols:
                conn.execute(f"ALTER TABLE client_campaigns ADD COLUMN {col} {ddl}")

        # Provenance JSON: field -> source label (Manual Client Setup Edit wins over strategy import)
        profile_cols = {
            str(r["name"])
            for r in conn.execute("PRAGMA table_info(client_profiles)").fetchall()
        }
        if profile_cols and "field_provenance" not in profile_cols:
            conn.execute(
                "ALTER TABLE client_profiles ADD COLUMN field_provenance TEXT NOT NULL DEFAULT '{}'"
            )

        audit_cols = {
            str(r["name"])
            for r in conn.execute("PRAGMA table_info(client_setup_audit)").fetchall()
        }
        if audit_cols and "change_source" not in audit_cols:
            conn.execute(
                "ALTER TABLE client_setup_audit ADD COLUMN change_source TEXT NOT NULL DEFAULT ''"
            )

        # Research runs: optional campaign context
        run_cols = {
            str(r["name"])
            for r in conn.execute("PRAGMA table_info(company_research_runs)").fetchall()
        }
        if run_cols and "campaign_id" not in run_cols:
            conn.execute(
                "ALTER TABLE company_research_runs ADD COLUMN campaign_id INTEGER"
            )

        # Opportunity assignments: retain source/target campaign for future imports
        oa_cols = {
            str(r["name"])
            for r in conn.execute("PRAGMA table_info(opportunity_assignments)").fetchall()
        }
        if oa_cols:
            if "target_campaign_id" not in oa_cols:
                conn.execute(
                    "ALTER TABLE opportunity_assignments "
                    "ADD COLUMN target_campaign_id INTEGER"
                )
            if "source_campaign_id" not in oa_cols:
                conn.execute(
                    "ALTER TABLE opportunity_assignments "
                    "ADD COLUMN source_campaign_id INTEGER"
                )

        _migrate_profiles_to_campaigns(conn)
        if owns:
            conn.commit()
    finally:
        if owns:
            conn.close()


def _client_slug(code: str, name: str) -> str:
    blob = f"{code} {name}".lower()
    if "carmeco" in blob:
        return "carmeco"
    if "brown" in blob:
        return "brown"
    return ""


def _migrate_profiles_to_campaigns(conn) -> None:
    """One-time style migrate: create profile + default campaign from target_profiles.

    Never invent Brown fields. Carmeco → campaign 'Stamping' (default).
    Skip clients that already have campaigns.
    """
    clients = conn.execute("SELECT id, code, name FROM clients").fetchall()
    for cl in clients:
        cid = int(cl["id"])
        code = _blank(cl["code"])
        name = _blank(cl["name"])
        slug = _client_slug(code, name)

        existing_camps = conn.execute(
            "SELECT id FROM client_campaigns WHERE client_id = ? LIMIT 1",
            (cid,),
        ).fetchone()
        if existing_camps:
            # Still ensure client_profiles row exists
            _ensure_profile_row(conn, cid)
            continue

        tp = conn.execute(
            "SELECT * FROM client_target_profiles WHERE client_id = ?",
            (cid,),
        ).fetchone()
        tp_dict = dict(tp) if tp else {}

        # Client profile (overview + sells) — only from known data
        primary = _blank(tp_dict.get("primary_service"))
        secondary = _blank(tp_dict.get("secondary_services"))
        products = _blank(tp_dict.get("target_products"))
        description = _blank(tp_dict.get("summary"))
        equipment = _blank(tp_dict.get("stamping_capability"))
        value_prop = ""
        if slug == "carmeco" and description:
            value_prop = description

        conn.execute(
            """
            INSERT OR IGNORE INTO client_profiles (
                client_id, website, main_location, main_phone, description,
                primary_owner_name, is_active, primary_service, secondary_services,
                products_services, differentiators, certifications,
                equipment_capacity, value_proposition, created_at, updated_at
            ) VALUES (?, '', '', '', ?, '', 1, ?, ?, ?, '', '', ?, ?, ?, ?)
            """,
            (
                cid,
                description,
                primary,
                secondary,
                products,
                equipment,
                value_prop,
                _now(),
                _now(),
            ),
        )

        # Campaign name: Carmeco → Stamping; others → Default
        campaign_name = "Stamping" if slug == "carmeco" else "Default"
        geography = _blank(tp_dict.get("geographic_preferences"))
        geo_mode = ""
        if geography:
            gl = geography.lower()
            if "nationwide" in gl or "national" in gl:
                geo_mode = "nationwide"
            elif "radius" in gl or "mile" in gl:
                geo_mode = "radius"
            elif "state" in gl or "region" in gl:
                geo_mode = "states"

        cur = conn.execute(
            """
            INSERT INTO client_campaigns (
                client_id, campaign_name, description, is_active, is_default,
                primary_service, secondary_services, target_industries,
                target_customer_types, target_products,
                manufacturing_processes_sought, production_preference,
                stamping_capability, tooling_notes, geographic_preferences,
                geography_mode, geography_required, company_size_preferences,
                positive_signals, negative_signals, exclusions, target_titles,
                fit_weighting_notes, notes, created_at, updated_at
            ) VALUES (
                ?, ?, ?, 1, 1,
                ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, 0, ?,
                ?, ?, '', '',
                ?, ?, ?, ?
            )
            """,
            (
                cid,
                campaign_name,
                _blank(tp_dict.get("notes")) or description,
                primary,
                secondary,
                _blank(tp_dict.get("target_industries")),
                _blank(tp_dict.get("ideal_customer_types")),
                products,
                _blank(tp_dict.get("manufacturing_processes_sought")),
                _blank(tp_dict.get("production_preference")),
                _blank(tp_dict.get("stamping_capability")),
                _blank(tp_dict.get("tooling_notes")),
                geography,
                geo_mode,
                _blank(tp_dict.get("company_size_preferences")),
                _blank(tp_dict.get("positive_fit_signals")),
                _blank(tp_dict.get("negative_fit_signals")),
                _blank(tp_dict.get("fit_weighting_notes")),
                _blank(tp_dict.get("notes")),
                _now(),
                _now(),
            ),
        )
        campaign_id = int(cur.lastrowid)
        conn.execute(
            """
            UPDATE client_profiles
            SET default_campaign_id = ?, updated_at = ?
            WHERE client_id = ?
            """,
            (campaign_id, _now(), cid),
        )
        # Keep legacy target_profiles aligned with default campaign
        _sync_legacy_target_profile(conn, cid, campaign_id)


def _ensure_profile_row(conn, client_id: int) -> None:
    row = conn.execute(
        "SELECT client_id FROM client_profiles WHERE client_id = ?",
        (client_id,),
    ).fetchone()
    if row:
        return
    conn.execute(
        """
        INSERT INTO client_profiles (client_id, created_at, updated_at)
        VALUES (?, ?, ?)
        """,
        (client_id, _now(), _now()),
    )


def user_can_edit_client_setup(user_id: int, client_id: int) -> bool:
    """Admin / management / Rev Ops (and authorized setup editors) can edit; others view-only."""
    user = get_user_by_id(user_id)
    if user is None or not user.active:
        return False
    if user.is_administrator:
        return True
    if not user_can_access_client(user_id, client_id):
        return False
    # Explicit authorized Client Setup editors (ICP config — not CRM mutation)
    email = _blank(user.email).lower()
    if email in {
        DEFAULT_USER_EMAIL.lower(),
        "admin@northstargroup.com",
    }:
        return True
    for a in list_clients_for_user(user_id, active_only=True):
        if a.client_id == client_id:
            role = _blank(a.role).lower().replace(" ", "_").replace("-", "_")
            if role in EDIT_ROLES:
                return True
            if "management" in role or "ops" in role or "admin" in role:
                return True
    return False


def _audit(
    conn,
    *,
    client_id: int,
    campaign_id: int | None,
    entity_type: str,
    field_name: str,
    old_value: str,
    new_value: str,
    user_id: int | None,
    user_name: str,
    change_source: str = MANUAL_EDIT_SOURCE,
) -> None:
    if _blank(old_value) == _blank(new_value):
        return
    conn.execute(
        """
        INSERT INTO client_setup_audit (
            client_id, campaign_id, entity_type, field_name,
            old_value, new_value, changed_by_user_id, changed_by_name, changed_at,
            change_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            client_id,
            campaign_id,
            entity_type,
            field_name,
            old_value,
            new_value,
            user_id,
            user_name,
            _now(),
            change_source or MANUAL_EDIT_SOURCE,
        ),
    )


def _load_field_provenance(conn, client_id: int) -> dict[str, str]:
    row = conn.execute(
        "SELECT field_provenance FROM client_profiles WHERE client_id = ?",
        (client_id,),
    ).fetchone()
    if not row:
        return {}
    raw = _blank(row["field_provenance"] if "field_provenance" in row.keys() else "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _set_field_provenance(
    conn,
    client_id: int,
    updates: dict[str, str],
) -> None:
    if not updates:
        return
    current = _load_field_provenance(conn, client_id)
    current.update(updates)
    conn.execute(
        """
        UPDATE client_profiles
        SET field_provenance = ?, updated_at = ?
        WHERE client_id = ?
        """,
        (json.dumps(current), _now(), client_id),
    )


def _campaign_row_to_view(row: Any) -> ClientCampaignView:
    d = dict(row)
    return ClientCampaignView(
        campaign_id=int(d["id"]),
        client_id=int(d["client_id"]),
        campaign_name=_blank(d.get("campaign_name")),
        description=_blank(d.get("description")),
        active=bool(d.get("is_active", 1)),
        is_default=bool(d.get("is_default", 0)),
        primary_service=_blank(d.get("primary_service")),
        secondary_services=_blank(d.get("secondary_services")),
        target_industries=_blank(d.get("target_industries")),
        target_customer_types=_blank(d.get("target_customer_types")),
        target_products=_blank(d.get("target_products")),
        manufacturing_processes_sought=_blank(d.get("manufacturing_processes_sought")),
        production_preference=_blank(d.get("production_preference")),
        stamping_capability=_blank(d.get("stamping_capability")),
        tooling_notes=_blank(d.get("tooling_notes")),
        geographic_preferences=_blank(d.get("geographic_preferences")),
        geography_mode=_blank(d.get("geography_mode")),
        geography_required=bool(d.get("geography_required", 0)),
        company_size_preferences=_blank(d.get("company_size_preferences")),
        positive_signals=_blank(d.get("positive_signals")),
        negative_signals=_blank(d.get("negative_signals")),
        exclusions=_blank(d.get("exclusions")),
        target_titles=_blank(d.get("target_titles")),
        fit_weighting_notes=_blank(d.get("fit_weighting_notes")),
        notes=_blank(d.get("notes")),
        list_source=_blank(d.get("list_source")),
        import_date=_blank(d.get("import_date")),
        created_at=_blank(d.get("created_at")),
        updated_at=_blank(d.get("updated_at")),
    )


def campaign_to_fit_profile(campaign: dict[str, Any] | None) -> dict[str, str]:
    """Map campaign fields → legacy fit profile dict used by research_data._compute_fit."""
    if not campaign:
        return {}
    return {
        "summary": _blank(campaign.get("description"))
        or _blank(campaign.get("notes")),
        "target_industries": _blank(campaign.get("target_industries")),
        "target_capabilities": _blank(campaign.get("primary_service"))
        + (
            "; " + _blank(campaign.get("secondary_services"))
            if _blank(campaign.get("secondary_services"))
            else ""
        ),
        "target_products": _blank(campaign.get("target_products")),
        "notes": _blank(campaign.get("notes")),
        "primary_service": _blank(campaign.get("primary_service")),
        "secondary_services": _blank(campaign.get("secondary_services")),
        "ideal_customer_types": _blank(campaign.get("target_customer_types")),
        "manufacturing_processes_sought": _blank(
            campaign.get("manufacturing_processes_sought")
        ),
        "production_preference": _blank(campaign.get("production_preference")),
        "stamping_capability": _blank(campaign.get("stamping_capability")),
        "tooling_notes": _blank(campaign.get("tooling_notes")),
        "geographic_preferences": _blank(campaign.get("geographic_preferences")),
        "company_size_preferences": _blank(campaign.get("company_size_preferences")),
        "positive_fit_signals": _blank(campaign.get("positive_signals")),
        "negative_fit_signals": _blank(campaign.get("negative_signals"))
        or _blank(campaign.get("exclusions")),
        "fit_weighting_notes": _blank(campaign.get("fit_weighting_notes")),
        "target_titles": _blank(campaign.get("target_titles")),
        "campaign_id": str(campaign.get("id") or campaign.get("campaign_id") or ""),
        "campaign_name": _blank(campaign.get("campaign_name")),
    }


def get_default_campaign(conn, client_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM client_campaigns
        WHERE client_id = ? AND is_default = 1 AND is_active = 1
        ORDER BY id ASC LIMIT 1
        """,
        (client_id,),
    ).fetchone()
    if row:
        return dict(row)
    row = conn.execute(
        """
        SELECT * FROM client_campaigns
        WHERE client_id = ? AND is_active = 1
        ORDER BY is_default DESC, id ASC LIMIT 1
        """,
        (client_id,),
    ).fetchone()
    return dict(row) if row else None


def get_campaign_for_client(
    conn, client_id: int, campaign_id: int | None = None
) -> dict[str, Any] | None:
    if campaign_id:
        row = conn.execute(
            """
            SELECT * FROM client_campaigns
            WHERE id = ? AND client_id = ?
            """,
            (campaign_id, client_id),
        ).fetchone()
        if row:
            return dict(row)
    return get_default_campaign(conn, client_id)


def resolve_campaign_by_name(
    conn, client_id: int, campaign_name: str
) -> dict[str, Any] | None:
    """Resolve a named campaign without inventing names — exact/soft match only."""
    needle = _blank(campaign_name).lower()
    if not needle:
        return None
    rows = conn.execute(
        """
        SELECT * FROM client_campaigns
        WHERE client_id = ? AND is_active = 1
        ORDER BY is_default DESC, id ASC
        """,
        (client_id,),
    ).fetchall()
    for r in rows:
        name = _blank(r["campaign_name"]).lower()
        if name == needle:
            return dict(r)
    for r in rows:
        name = _blank(r["campaign_name"]).lower()
        if needle in name or name in needle:
            return dict(r)
    return None


def _sync_legacy_target_profile(conn, client_id: int, campaign_id: int) -> None:
    """Keep client_target_profiles in sync with the default campaign."""
    camp = conn.execute(
        "SELECT * FROM client_campaigns WHERE id = ? AND client_id = ?",
        (campaign_id, client_id),
    ).fetchone()
    if not camp:
        return
    profile = campaign_to_fit_profile(dict(camp))
    cols = (
        "summary",
        "target_industries",
        "target_capabilities",
        "target_products",
        "notes",
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
    existing = conn.execute(
        "SELECT client_id FROM client_target_profiles WHERE client_id = ?",
        (client_id,),
    ).fetchone()
    values = [profile.get(c, "") for c in cols]
    if existing:
        conn.execute(
            f"""
            UPDATE client_target_profiles SET
                {", ".join(f"{c} = ?" for c in cols)},
                updated_at = ?
            WHERE client_id = ?
            """,
            (*values, _now(), client_id),
        )
    else:
        conn.execute(
            f"""
            INSERT INTO client_target_profiles (
                client_id, {", ".join(cols)}, updated_at
            ) VALUES (?, {", ".join("?" for _ in cols)}, ?)
            """,
            (client_id, *values, _now()),
        )


def _weighted_percent(filled_weight: int, total_weight: int) -> int:
    if total_weight <= 0:
        return 0
    return int(round((filled_weight / total_weight) * 100))


def _campaign_eval_view(campaign: dict[str, Any] | None, profile: dict[str, Any]) -> dict[str, str]:
    """Normalize campaign + client sells into one eval dict for completeness / AI Fit."""
    camp = campaign or {}
    primary = _blank(camp.get("primary_service")) or _blank(profile.get("primary_service"))
    secondary = _blank(camp.get("secondary_services")) or _blank(
        profile.get("secondary_services")
    )
    products = _blank(camp.get("target_products")) or _blank(
        profile.get("products_services")
    )
    neg = _blank(camp.get("negative_signals"))
    excl = _blank(camp.get("exclusions"))
    neg_or = neg or excl
    geo = _blank(camp.get("geographic_preferences"))
    if not geo and _blank(camp.get("geography_mode")):
        geo = _blank(camp.get("geography_mode"))
    campaign_defined = ""
    if campaign and _blank(camp.get("campaign_name")):
        campaign_defined = _blank(camp.get("campaign_name"))
    return {
        "primary_service": primary,
        "secondary_services": secondary,
        "target_customer_types": _blank(camp.get("target_customer_types")),
        "target_industries": _blank(camp.get("target_industries")),
        "target_products": products,
        "manufacturing_processes_sought": _blank(
            camp.get("manufacturing_processes_sought")
        ),
        "production_preference": _blank(camp.get("production_preference")),
        "geographic_preferences": geo,
        "positive_signals": _blank(camp.get("positive_signals")),
        "negative_signals": neg,
        "exclusions": excl,
        "negative_or_exclusions": neg_or,
        "target_titles": _blank(camp.get("target_titles")),
        "campaign_defined": campaign_defined,
        "fit_weighting_notes": _blank(camp.get("fit_weighting_notes")),
    }


def resolve_known_client_info(
    conn,
    *,
    client_id: int,
    client_name: str,
    profile: dict[str, Any],
    campaign: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Soft-fill blank admin fields from existing NorthStar sources for display.

    Never overwrites non-blank stored profile values. Does not invent website/phone.
    Returns (effective_profile_fields, field_sources).
    """
    sources: dict[str, str] = {}
    effective = {
        "client_name": _blank(client_name),
        "website": _blank(profile.get("website")),
        "main_location": _blank(profile.get("main_location")),
        "main_phone": _blank(profile.get("main_phone")),
        "description": _blank(profile.get("description")),
        "primary_owner_name": _blank(profile.get("primary_owner_name")),
        "is_active": bool(profile.get("is_active", 1)),
        "is_active_set": "1",  # always configured
        "primary_service": _blank(profile.get("primary_service")),
        "secondary_services": _blank(profile.get("secondary_services")),
        "products_services": _blank(profile.get("products_services")),
        "differentiators": _blank(profile.get("differentiators")),
        "certifications": _blank(profile.get("certifications")),
        "equipment_capacity": _blank(profile.get("equipment_capacity")),
        "value_proposition": _blank(profile.get("value_proposition")),
    }
    if effective["client_name"]:
        sources["client_name"] = "clients.name"

    # Description from stored target profile / campaign notes (already migrated often)
    if not effective["description"]:
        tp = conn.execute(
            "SELECT summary, notes FROM client_target_profiles WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        if tp and _blank(tp["summary"]):
            effective["description"] = _blank(tp["summary"])
            sources["description"] = "client_target_profiles.summary"
        elif campaign and _blank(campaign.get("description")):
            effective["description"] = _blank(campaign.get("description"))
            sources["description"] = "client_campaigns.description"
        elif campaign and _blank(campaign.get("notes")):
            effective["description"] = _blank(campaign.get("notes"))
            sources["description"] = "client_campaigns.notes"
    elif _blank(profile.get("description")):
        sources["description"] = "client_profiles.description"

    # Sells blanks: prefer campaign, else leave empty (do not invent)
    if not effective["primary_service"] and campaign:
        if _blank(campaign.get("primary_service")):
            effective["primary_service"] = _blank(campaign.get("primary_service"))
            sources["primary_service"] = "client_campaigns.primary_service"
    elif effective["primary_service"]:
        sources["primary_service"] = "client_profiles.primary_service"

    if not effective["secondary_services"] and campaign:
        if _blank(campaign.get("secondary_services")):
            effective["secondary_services"] = _blank(campaign.get("secondary_services"))
            sources["secondary_services"] = "client_campaigns.secondary_services"

    if not effective["products_services"] and campaign:
        if _blank(campaign.get("target_products")):
            effective["products_services"] = _blank(campaign.get("target_products"))
            sources["products_services"] = "client_campaigns.target_products"

    if not effective["equipment_capacity"] and campaign:
        if _blank(campaign.get("stamping_capability")):
            effective["equipment_capacity"] = _blank(campaign.get("stamping_capability"))
            sources["equipment_capacity"] = "client_campaigns.stamping_capability"

    if not effective["value_proposition"] and effective["description"]:
        # Display-only: same narrative already known — mark provenance, do not invent new copy
        effective["value_proposition"] = effective["description"]
        sources["value_proposition"] = sources.get(
            "description", "client_profiles.description"
        )

    # Primary owner suggestion from active assignment (display only; not invented free text)
    if not effective["primary_owner_name"]:
        row = conn.execute(
            """
            SELECT u.full_name, COALESCE(uca.role, '') AS role
            FROM user_client_assignments uca
            JOIN users u ON u.id = uca.user_id
            WHERE uca.client_id = ? AND uca.active = 1 AND u.active = 1
              AND trim(u.full_name) != ''
              AND lower(u.full_name) NOT LIKE '%external test%'
              AND lower(u.email) NOT LIKE 'external%'
            ORDER BY
              CASE
                WHEN lower(COALESCE(uca.role, '')) LIKE '%owner%' THEN 0
                WHEN lower(COALESCE(uca.role, '')) LIKE '%account_executive%' THEN 1
                WHEN lower(COALESCE(uca.role, '')) LIKE '%manager%' THEN 2
                WHEN lower(COALESCE(uca.role, '')) LIKE '%admin%' THEN 3
                WHEN trim(COALESCE(uca.role, '')) = '' THEN 9
                ELSE 4
              END,
              u.full_name COLLATE NOCASE
            LIMIT 1
            """,
            (client_id,),
        ).fetchone()
        if row and _blank(row["full_name"]):
            effective["primary_owner_name"] = _blank(row["full_name"])
            sources["primary_owner_name"] = "user_client_assignments (display only)"

    return effective, sources


def compute_completeness(
    profile: dict[str, Any],
    campaign: dict[str, Any] | None,
    *,
    client_name: str = "",
    effective_info: dict[str, Any] | None = None,
) -> ClientSetupCompleteness:
    """Client Information % + Target Profile % + AI Fit readiness.

    Administrative Client Information gaps do NOT set ai_fit_ready=False.
    """
    info = effective_info or {
        "client_name": client_name or _blank(profile.get("client_name")),
        "website": _blank(profile.get("website")),
        "main_location": _blank(profile.get("main_location")),
        "main_phone": _blank(profile.get("main_phone")),
        "description": _blank(profile.get("description")),
        "primary_owner_name": _blank(profile.get("primary_owner_name")),
        "is_active_set": "1",
        "primary_service": _blank(profile.get("primary_service")),
        "secondary_services": _blank(profile.get("secondary_services")),
        "products_services": _blank(profile.get("products_services")),
    }
    if not _blank(info.get("client_name")) and client_name:
        info["client_name"] = client_name
    info["is_active_set"] = "1"

    # --- Client Information ---
    ci_filled_w = 0
    ci_total_w = 0
    ci_missing: list[str] = []
    for key, label, weight in CLIENT_INFO_FIELDS:
        ci_total_w += weight
        if _blank(info.get(key)):
            ci_filled_w += weight
        else:
            ci_missing.append(label)
    ci_pct = _weighted_percent(ci_filled_w, ci_total_w)

    # --- Target Profile ---
    eval_v = _campaign_eval_view(campaign, {**profile, **info})
    tp_filled_w = 0
    tp_total_w = 0
    tp_missing: list[str] = []
    for key, label, weight in TARGET_PROFILE_WEIGHTED:
        tp_total_w += weight
        if _blank(eval_v.get(key)):
            tp_filled_w += weight
        else:
            tp_missing.append(label)
    tp_pct = _weighted_percent(tp_filled_w, tp_total_w)

    # --- AI Fit readiness (critical targeting only) ---
    ai_missing: list[str] = []
    for key, label in AI_FIT_CRITICAL:
        if not _blank(eval_v.get(key)):
            ai_missing.append(label)
    ai_ready = len(ai_missing) == 0 and bool(campaign)

    # Intentionally unspecified — deliberate blanks, not readiness errors
    intentionally: list[str] = []
    if campaign is not None:
        if not _blank(campaign.get("company_size_preferences")):
            intentionally.append(
                "Company size preference — not established (left unspecified by design)"
            )
        if not _blank(campaign.get("exclusions")):
            intentionally.append(
                "Absolute exclusions — none currently established (do not invent)"
            )

    missing_sections: list[str] = []
    if ci_missing:
        missing_sections.append("Client Information")
    if tp_missing:
        missing_sections.append("Target Profile")
    if not ai_ready:
        missing_sections.append("AI Fit Readiness")

    missing_fields = (
        [f"Client Information: {m}" for m in ci_missing]
        + [f"Target Profile: {m}" for m in tp_missing]
        + [f"AI Fit: {m}" for m in ai_missing]
    )

    # Aggregate for legacy list display: average of the two completeness scores
    aggregate = int(round((ci_pct + tp_pct) / 2)) if (ci_total_w and tp_total_w) else max(ci_pct, tp_pct)

    return ClientSetupCompleteness(
        percent=aggregate,
        filled_fields=ci_filled_w + tp_filled_w,
        total_fields=ci_total_w + tp_total_w,
        missing_sections=missing_sections,
        missing_fields=missing_fields,
        strong_fit_ready=ai_ready,
        client_information_percent=ci_pct,
        client_information_missing=ci_missing,
        target_profile_percent=tp_pct,
        target_profile_missing=tp_missing,
        ai_fit_ready=ai_ready,
        ai_fit_missing=ai_missing,
        intentionally_unspecified=intentionally,
    )


def list_client_setup_summaries(*, user_id: int | None = None) -> list[ClientListSetupItem]:
    ensure_client_setup_schema()
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    assignments = list_clients_for_user(user.id, active_only=True)
    allowed = {a.client_id for a in assignments}
    if not allowed and not user.is_administrator:
        return []

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, code, name FROM clients ORDER BY name COLLATE NOCASE"
        ).fetchall()
        out: list[ClientListSetupItem] = []
        for r in rows:
            cid = int(r["id"])
            if not user.is_administrator and cid not in allowed:
                continue
            _ensure_profile_row(conn, cid)
            profile = dict(
                conn.execute(
                    "SELECT * FROM client_profiles WHERE client_id = ?", (cid,)
                ).fetchone()
                or {"client_id": cid}
            )
            camp = get_default_campaign(conn, cid)
            effective, _sources = resolve_known_client_info(
                conn,
                client_id=cid,
                client_name=_blank(r["name"]),
                profile=profile,
                campaign=camp,
            )
            completeness = compute_completeness(
                profile,
                camp,
                client_name=_blank(r["name"]),
                effective_info=effective,
            )
            camps = conn.execute(
                "SELECT COUNT(*) AS n FROM client_campaigns WHERE client_id = ?",
                (cid,),
            ).fetchone()
            out.append(
                ClientListSetupItem(
                    client_id=cid,
                    client_code=_blank(r["code"]),
                    client_name=_blank(r["name"]),
                    is_active=bool(profile.get("is_active", 1)),
                    completeness_percent=completeness.target_profile_percent,
                    client_information_percent=completeness.client_information_percent,
                    target_profile_percent=completeness.target_profile_percent,
                    ai_fit_ready=completeness.ai_fit_ready,
                    missing_sections=completeness.missing_sections,
                    campaign_count=int(camps["n"]) if camps else 0,
                    default_campaign_name=_blank((camp or {}).get("campaign_name")),
                    can_edit=user_can_edit_client_setup(user.id, cid),
                )
            )
        conn.commit()
    return out


def get_client_setup(
    client_id: int, *, user_id: int | None = None
) -> ClientSetupResponse:
    ensure_client_setup_schema()
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    if not user_can_access_client(user.id, client_id) and not user.is_administrator:
        raise PermissionError("Not authorized for this client.")

    with get_connection() as conn:
        cl = conn.execute(
            "SELECT id, code, name FROM clients WHERE id = ?", (client_id,)
        ).fetchone()
        if not cl:
            raise LookupError("Client not found.")
        _ensure_profile_row(conn, client_id)
        profile = dict(
            conn.execute(
                "SELECT * FROM client_profiles WHERE client_id = ?", (client_id,)
            ).fetchone()
        )
        camp_rows = conn.execute(
            """
            SELECT * FROM client_campaigns
            WHERE client_id = ?
            ORDER BY is_default DESC, campaign_name COLLATE NOCASE
            """,
            (client_id,),
        ).fetchall()
        campaigns = [_campaign_row_to_view(r) for r in camp_rows]
        default_camp = get_default_campaign(conn, client_id)
        effective, field_sources = resolve_known_client_info(
            conn,
            client_id=client_id,
            client_name=_blank(cl["name"]),
            profile=profile,
            campaign=default_camp,
        )
        # Manual edits are approved current profile — surface provenance for strategy-import compare
        provenance = _load_field_provenance(conn, client_id)
        for key, src in provenance.items():
            if src == MANUAL_EDIT_SOURCE:
                field_sources[key] = MANUAL_EDIT_SOURCE
        completeness = compute_completeness(
            profile,
            default_camp,
            client_name=_blank(cl["name"]),
            effective_info=effective,
        )
        audit_rows = conn.execute(
            """
            SELECT * FROM client_setup_audit
            WHERE client_id = ?
            ORDER BY changed_at DESC, id DESC
            LIMIT 50
            """,
            (client_id,),
        ).fetchall()
        audit = [
            ClientSetupAuditItem(
                id=int(a["id"]),
                client_id=int(a["client_id"]),
                campaign_id=_as_int(a["campaign_id"]) or None,
                entity_type=_blank(a["entity_type"]),
                field_name=_blank(a["field_name"]),
                old_value=_blank(a["old_value"]),
                new_value=_blank(a["new_value"]),
                changed_by_name=_blank(a["changed_by_name"]),
                changed_at=_blank(a["changed_at"]),
                change_source=_blank(
                    a["change_source"] if "change_source" in a.keys() else ""
                )
                or MANUAL_EDIT_SOURCE,
            )
            for a in audit_rows
        ]
        conn.commit()

    return ClientSetupResponse(
        client_id=client_id,
        client_code=_blank(cl["code"]),
        client_name=_blank(cl["name"]),
        website=_blank(effective.get("website")),
        main_location=_blank(effective.get("main_location")),
        main_phone=_blank(effective.get("main_phone")),
        description=_blank(effective.get("description")),
        primary_owner_name=_blank(effective.get("primary_owner_name")),
        is_active=bool(profile.get("is_active", 1)),
        primary_service=_blank(effective.get("primary_service")),
        secondary_services=_blank(effective.get("secondary_services")),
        products_services=_blank(effective.get("products_services")),
        differentiators=_blank(profile.get("differentiators")),
        certifications=_blank(profile.get("certifications")),
        equipment_capacity=_blank(effective.get("equipment_capacity")),
        value_proposition=_blank(effective.get("value_proposition")),
        default_campaign_id=_as_int(profile.get("default_campaign_id")) or (
            int(default_camp["id"]) if default_camp else None
        ),
        campaigns=campaigns,
        completeness=completeness,
        field_sources=field_sources,
        can_edit=user_can_edit_client_setup(user.id, client_id),
        audit_history=audit,
        updated_at=_blank(profile.get("updated_at")),
    )


def update_client_overview(
    client_id: int,
    body: ClientOverviewUpdate,
    *,
    user_id: int | None = None,
) -> ClientSetupResponse:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    require_write_client_id(client_id, user_id=user.id)
    if not user_can_edit_client_setup(user.id, client_id):
        raise PermissionError("Not authorized to edit client setup.")

    ensure_client_setup_schema()
    with get_connection() as conn:
        client = conn.execute(
            "SELECT id, name FROM clients WHERE id = ?", (client_id,)
        ).fetchone()
        if not client:
            raise LookupError("Client not found.")
        _ensure_profile_row(conn, client_id)
        old = dict(
            conn.execute(
                "SELECT * FROM client_profiles WHERE client_id = ?", (client_id,)
            ).fetchone()
        )

        # Only write fields that actually changed for this client_id
        profile_updates: dict[str, Any] = {}
        provenance_updates: dict[str, str] = {}

        text_fields = {
            "website": _blank(body.website),
            "main_location": _blank(body.main_location),
            "main_phone": _blank(body.main_phone),
            "description": _blank(body.description),
            "primary_owner_name": _blank(body.primary_owner_name),
        }
        for key, new_v in text_fields.items():
            old_v = _blank(old.get(key))
            if old_v == new_v:
                continue
            profile_updates[key] = new_v
            _audit(
                conn,
                client_id=client_id,
                campaign_id=None,
                entity_type="client_profile",
                field_name=key,
                old_value=old_v,
                new_value=new_v,
                user_id=user.id,
                user_name=_blank(user.full_name) or _blank(user.email),
                change_source=MANUAL_EDIT_SOURCE,
            )
            provenance_updates[key] = MANUAL_EDIT_SOURCE

        new_active = 1 if body.is_active else 0
        old_active = int(old.get("is_active") or 0)
        if new_active != old_active:
            profile_updates["is_active"] = new_active
            _audit(
                conn,
                client_id=client_id,
                campaign_id=None,
                entity_type="client_profile",
                field_name="is_active",
                old_value=str(old_active),
                new_value=str(new_active),
                user_id=user.id,
                user_name=_blank(user.full_name) or _blank(user.email),
                change_source=MANUAL_EDIT_SOURCE,
            )
            provenance_updates["is_active"] = MANUAL_EDIT_SOURCE

        new_name = _blank(body.client_name)
        old_name = _blank(client["name"])
        if new_name and new_name != old_name:
            # Rename only this client row — never touch another client_id
            conn.execute(
                "UPDATE clients SET name = ? WHERE id = ?",
                (new_name, client_id),
            )
            _audit(
                conn,
                client_id=client_id,
                campaign_id=None,
                entity_type="client",
                field_name="client_name",
                old_value=old_name,
                new_value=new_name,
                user_id=user.id,
                user_name=_blank(user.full_name) or _blank(user.email),
                change_source=MANUAL_EDIT_SOURCE,
            )
            provenance_updates["client_name"] = MANUAL_EDIT_SOURCE

        if profile_updates:
            sets = ", ".join(f"{k} = ?" for k in profile_updates)
            vals = list(profile_updates.values())
            vals.extend([_now(), client_id])
            conn.execute(
                f"UPDATE client_profiles SET {sets}, updated_at = ? WHERE client_id = ?",
                vals,
            )
        if provenance_updates:
            _set_field_provenance(conn, client_id, provenance_updates)
        conn.commit()
    return get_client_setup(client_id, user_id=user.id)


def update_client_sells(
    client_id: int,
    body: ClientSellsUpdate,
    *,
    user_id: int | None = None,
) -> ClientSetupResponse:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    require_write_client_id(client_id, user_id=user.id)
    if not user_can_edit_client_setup(user.id, client_id):
        raise PermissionError("Not authorized to edit client setup.")

    ensure_client_setup_schema()
    payload = {
        "primary_service": _blank(body.primary_service),
        "secondary_services": _blank(body.secondary_services),
        "products_services": _blank(body.products_services),
        "differentiators": _blank(body.differentiators),
        "certifications": _blank(body.certifications),
        "equipment_capacity": _blank(body.equipment_capacity),
        "value_proposition": _blank(body.value_proposition),
    }
    with get_connection() as conn:
        if not conn.execute(
            "SELECT id FROM clients WHERE id = ?", (client_id,)
        ).fetchone():
            raise LookupError("Client not found.")
        _ensure_profile_row(conn, client_id)
        old = dict(
            conn.execute(
                "SELECT * FROM client_profiles WHERE client_id = ?", (client_id,)
            ).fetchone()
        )
        profile_updates: dict[str, str] = {}
        provenance_updates: dict[str, str] = {}
        for key, new_v in payload.items():
            old_v = _blank(old.get(key))
            if old_v == new_v:
                continue
            profile_updates[key] = new_v
            _audit(
                conn,
                client_id=client_id,
                campaign_id=None,
                entity_type="client_profile",
                field_name=key,
                old_value=old_v,
                new_value=new_v,
                user_id=user.id,
                user_name=_blank(user.full_name) or _blank(user.email),
                change_source=MANUAL_EDIT_SOURCE,
            )
            provenance_updates[key] = MANUAL_EDIT_SOURCE

        if profile_updates:
            sets = ", ".join(f"{k} = ?" for k in profile_updates)
            vals = list(profile_updates.values())
            vals.extend([_now(), client_id])
            conn.execute(
                f"UPDATE client_profiles SET {sets}, updated_at = ? WHERE client_id = ?",
                vals,
            )
            # If default campaign has empty primary_service, mirror client primary
            if "primary_service" in profile_updates and profile_updates["primary_service"]:
                default = get_default_campaign(conn, client_id)
                if default and not _blank(default.get("primary_service")):
                    conn.execute(
                        """
                        UPDATE client_campaigns SET primary_service = ?, updated_at = ?
                        WHERE id = ? AND client_id = ?
                        """,
                        (
                            profile_updates["primary_service"],
                            _now(),
                            int(default["id"]),
                            client_id,
                        ),
                    )
                    _sync_legacy_target_profile(conn, client_id, int(default["id"]))
        if provenance_updates:
            _set_field_provenance(conn, client_id, provenance_updates)
        conn.commit()
    return get_client_setup(client_id, user_id=user.id)


def create_campaign(
    client_id: int,
    body: ClientCampaignUpdate,
    *,
    user_id: int | None = None,
) -> ClientSetupResponse:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    require_write_client_id(client_id, user_id=user.id)
    if not user_can_edit_client_setup(user.id, client_id):
        raise PermissionError("Not authorized to edit client setup.")
    name = _blank(body.campaign_name)
    if not name:
        raise ValueError("Campaign name is required.")

    ensure_client_setup_schema()
    with get_connection() as conn:
        # Verify client
        if not conn.execute(
            "SELECT id FROM clients WHERE id = ?", (client_id,)
        ).fetchone():
            raise LookupError("Client not found.")
        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM client_campaigns WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        make_default = bool(body.is_default) or (int(existing["n"]) == 0)
        if make_default:
            conn.execute(
                "UPDATE client_campaigns SET is_default = 0 WHERE client_id = ?",
                (client_id,),
            )
        cur = conn.execute(
            """
            INSERT INTO client_campaigns (
                client_id, campaign_name, description, is_active, is_default,
                primary_service, secondary_services, target_industries,
                target_customer_types, target_products,
                manufacturing_processes_sought, production_preference,
                stamping_capability, tooling_notes, geographic_preferences,
                geography_mode, geography_required, company_size_preferences,
                positive_signals, negative_signals, exclusions, target_titles,
                fit_weighting_notes, notes, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?
            )
            """,
            (
                client_id,
                name,
                _blank(body.description),
                1 if body.active else 0,
                1 if make_default else 0,
                _blank(body.primary_service),
                _blank(body.secondary_services),
                _blank(body.target_industries),
                _blank(body.target_customer_types),
                _blank(body.target_products),
                _blank(body.manufacturing_processes_sought),
                _blank(body.production_preference),
                _blank(body.stamping_capability),
                _blank(body.tooling_notes),
                _blank(body.geographic_preferences),
                _blank(body.geography_mode),
                1 if body.geography_required else 0,
                _blank(body.company_size_preferences),
                _blank(body.positive_signals),
                _blank(body.negative_signals),
                _blank(body.exclusions),
                _blank(body.target_titles),
                _blank(body.fit_weighting_notes),
                _blank(body.notes),
                _now(),
                _now(),
            ),
        )
        campaign_id = int(cur.lastrowid)
        _audit(
            conn,
            client_id=client_id,
            campaign_id=campaign_id,
            entity_type="client_campaign",
            field_name="campaign_created",
            old_value="",
            new_value=name,
            user_id=user.id,
            user_name=user.full_name,
        )
        if make_default:
            conn.execute(
                """
                UPDATE client_profiles
                SET default_campaign_id = ?, updated_at = ?
                WHERE client_id = ?
                """,
                (campaign_id, _now(), client_id),
            )
            _sync_legacy_target_profile(conn, client_id, campaign_id)
        conn.commit()
    return get_client_setup(client_id, user_id=user.id)


def update_campaign(
    client_id: int,
    campaign_id: int,
    body: ClientCampaignUpdate,
    *,
    user_id: int | None = None,
) -> ClientSetupResponse:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    require_write_client_id(client_id, user_id=user.id)
    if not user_can_edit_client_setup(user.id, client_id):
        raise PermissionError("Not authorized to edit client setup.")

    ensure_client_setup_schema()
    with get_connection() as conn:
        old_row = conn.execute(
            "SELECT * FROM client_campaigns WHERE id = ? AND client_id = ?",
            (campaign_id, client_id),
        ).fetchone()
        if not old_row:
            raise LookupError("Campaign not found for this client.")
        old = dict(old_row)
        desired = {
            "campaign_name": _blank(body.campaign_name) or _blank(old.get("campaign_name")),
            "description": _blank(body.description),
            "is_active": 1 if body.active else 0,
            "is_default": 1 if body.is_default else int(old.get("is_default") or 0),
            "primary_service": _blank(body.primary_service),
            "secondary_services": _blank(body.secondary_services),
            "target_industries": _blank(body.target_industries),
            "target_customer_types": _blank(body.target_customer_types),
            "target_products": _blank(body.target_products),
            "manufacturing_processes_sought": _blank(body.manufacturing_processes_sought),
            "production_preference": _blank(body.production_preference),
            "stamping_capability": _blank(body.stamping_capability),
            "tooling_notes": _blank(body.tooling_notes),
            "geographic_preferences": _blank(body.geographic_preferences),
            "geography_mode": _blank(body.geography_mode),
            "geography_required": 1 if body.geography_required else 0,
            "company_size_preferences": _blank(body.company_size_preferences),
            "positive_signals": _blank(body.positive_signals),
            "negative_signals": _blank(body.negative_signals),
            "exclusions": _blank(body.exclusions),
            "target_titles": _blank(body.target_titles),
            "fit_weighting_notes": _blank(body.fit_weighting_notes),
            "notes": _blank(body.notes),
        }
        if body.is_default:
            desired["is_default"] = 1

        changes: dict[str, Any] = {}
        for key, new_v in desired.items():
            old_v = old.get(key)
            old_s = str(old_v if old_v is not None else "")
            new_s = str(new_v)
            if old_s == new_s:
                continue
            changes[key] = new_v
            _audit(
                conn,
                client_id=client_id,
                campaign_id=campaign_id,
                entity_type="client_campaign",
                field_name=key,
                old_value=old_s,
                new_value=new_s,
                user_id=user.id,
                user_name=_blank(user.full_name) or _blank(user.email),
                change_source=MANUAL_EDIT_SOURCE,
            )

        if changes.get("is_default") == 1:
            conn.execute(
                "UPDATE client_campaigns SET is_default = 0 WHERE client_id = ? AND id != ?",
                (client_id, campaign_id),
            )

        if changes:
            sets = ", ".join(f"{k} = ?" for k in changes)
            vals = list(changes.values())
            vals.extend([_now(), campaign_id, client_id])
            conn.execute(
                f"UPDATE client_campaigns SET {sets}, updated_at = ? WHERE id = ? AND client_id = ?",
                vals,
            )
            still_default = (
                int(desired.get("is_default") or 0) == 1
                or int(old.get("is_default") or 0) == 1
            )
            if int(desired.get("is_default") or 0) == 1:
                conn.execute(
                    "UPDATE client_profiles SET default_campaign_id = ?, updated_at = ? WHERE client_id = ?",
                    (campaign_id, _now(), client_id),
                )
            if still_default:
                _sync_legacy_target_profile(conn, client_id, campaign_id)
        conn.commit()
    return get_client_setup(client_id, user_id=user.id)



def set_default_campaign(
    client_id: int,
    campaign_id: int,
    *,
    user_id: int | None = None,
) -> ClientSetupResponse:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    require_write_client_id(client_id, user_id=user.id)
    if not user_can_edit_client_setup(user.id, client_id):
        raise PermissionError("Not authorized to edit client setup.")

    ensure_client_setup_schema()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, campaign_name FROM client_campaigns WHERE id = ? AND client_id = ?",
            (campaign_id, client_id),
        ).fetchone()
        if not row:
            raise LookupError("Campaign not found for this client.")
        old_default = get_default_campaign(conn, client_id)
        conn.execute(
            "UPDATE client_campaigns SET is_default = 0 WHERE client_id = ?",
            (client_id,),
        )
        conn.execute(
            "UPDATE client_campaigns SET is_default = 1, updated_at = ? WHERE id = ?",
            (_now(), campaign_id),
        )
        conn.execute(
            """
            UPDATE client_profiles
            SET default_campaign_id = ?, updated_at = ?
            WHERE client_id = ?
            """,
            (campaign_id, _now(), client_id),
        )
        _audit(
            conn,
            client_id=client_id,
            campaign_id=campaign_id,
            entity_type="client_campaign",
            field_name="is_default",
            old_value=_blank((old_default or {}).get("campaign_name")),
            new_value=_blank(row["campaign_name"]),
            user_id=user.id,
            user_name=user.full_name,
        )
        _sync_legacy_target_profile(conn, client_id, campaign_id)
        conn.commit()
    return get_client_setup(client_id, user_id=user.id)


def delete_campaign(
    client_id: int,
    campaign_id: int,
    *,
    user_id: int | None = None,
) -> ClientSetupResponse:
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    require_write_client_id(client_id, user_id=user.id)
    if not user_can_edit_client_setup(user.id, client_id):
        raise PermissionError("Not authorized to edit client setup.")

    ensure_client_setup_schema()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM client_campaigns WHERE id = ? AND client_id = ?",
            (campaign_id, client_id),
        ).fetchone()
        if not row:
            raise LookupError("Campaign not found for this client.")
        was_default = bool(row["is_default"])
        name = _blank(row["campaign_name"])
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM client_campaigns WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        if int(count["n"]) <= 1:
            raise ValueError("Cannot delete the only campaign for a client.")
        conn.execute(
            "DELETE FROM client_campaigns WHERE id = ? AND client_id = ?",
            (campaign_id, client_id),
        )
        _audit(
            conn,
            client_id=client_id,
            campaign_id=None,
            entity_type="client_campaign",
            field_name="campaign_deleted",
            old_value=name,
            new_value="",
            user_id=user.id,
            user_name=user.full_name,
        )
        if was_default:
            nxt = conn.execute(
                """
                SELECT id FROM client_campaigns
                WHERE client_id = ?
                ORDER BY id ASC LIMIT 1
                """,
                (client_id,),
            ).fetchone()
            if nxt:
                nid = int(nxt["id"])
                conn.execute(
                    "UPDATE client_campaigns SET is_default = 1 WHERE id = ?",
                    (nid,),
                )
                conn.execute(
                    """
                    UPDATE client_profiles
                    SET default_campaign_id = ?, updated_at = ?
                    WHERE client_id = ?
                    """,
                    (nid, _now(), client_id),
                )
                _sync_legacy_target_profile(conn, client_id, nid)
        conn.commit()
    return get_client_setup(client_id, user_id=user.id)
