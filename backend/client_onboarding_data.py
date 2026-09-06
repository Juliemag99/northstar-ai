"""Administrator client onboarding wizard — drafts, templates, copy preview, finish.

Writes are client-scoped, audited, and transactional. Blank draft fields never
erase populated destination profile/campaign values unless overwrite is confirmed.
Never invents capabilities. Never copies prospects, CRM masters, credentials,
email connections, users, assignments, research, or audit history.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from access import get_user_by_id, require_write_client_id, user_can_access_client
from client_onboarding_templates import (
    COPYABLE_CRM_FIELDS,
    COPYABLE_OPPORTUNITY_FIELDS,
    COPYABLE_SELLS_FIELDS,
    FORBIDDEN_COPY_CATEGORIES,
    get_template,
    list_templates,
)
from client_setup_data import (
    _audit,
    _ensure_profile_row,
    ensure_client_setup_schema,
    get_client_setup,
)
from db import get_connection
from models import NorthStarUser

ONBOARDING_SOURCE = "Client Onboarding Wizard"
COPY_SOURCE_PREFIX = "Onboarding Copy"
TEMPLATE_SOURCE_PREFIX = "Onboarding Template"

REQUIRED_BASICS = ("client_name",)
RECOMMENDED_BASICS = ("website", "main_location", "description")
OPTIONAL_BASICS = ("client_code",)

REQUIRED_SELLS = ("primary_service",)
RECOMMENDED_SELLS = (
    "products_services",
    "secondary_services",
    "industries_served",
)
OPTIONAL_SELLS = ("equipment_capacity", "materials", "certifications", "differentiators")

REQUIRED_OPPS = ("target_customer_types",)
RECOMMENDED_OPPS = (
    "target_industries",
    "target_products",
    "manufacturing_processes_sought",
    "positive_signals",
    "negative_signals",
)
OPTIONAL_OPPS = (
    "production_preference",
    "geographic_preferences",
    "exclusions",
    "target_titles",
    "fit_weighting_notes",
)

REQUIRED_CRM = ("campaign_name", "statuses", "default_status")
RECOMMENDED_CRM: tuple[str, ...] = ("campaign_description",)
OPTIONAL_CRM = ("apply_assignments", "assignment_user_ids")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _slug_code(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", _blank(name).lower()).strip("-")
    return (base or "client")[:48]


def ensure_client_onboarding_schema(conn: sqlite3.Connection | None = None) -> None:
    owns = conn is None
    if owns:
        conn = get_connection()
    assert conn is not None
    try:
        ensure_client_setup_schema(conn)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS client_onboarding_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER,
                mode TEXT NOT NULL DEFAULT 'new',
                status TEXT NOT NULL DEFAULT 'draft',
                current_step INTEGER NOT NULL DEFAULT 1,
                payload_json TEXT NOT NULL DEFAULT '{}',
                template_id TEXT NOT NULL DEFAULT '',
                copy_source_client_id INTEGER,
                completion_percent INTEGER NOT NULL DEFAULT 0,
                created_by_user_id INTEGER,
                updated_by_user_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE SET NULL,
                FOREIGN KEY (copy_source_client_id) REFERENCES clients(id) ON DELETE SET NULL,
                FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY (updated_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_onboarding_drafts_client
                ON client_onboarding_drafts(client_id, status, updated_at DESC);

            CREATE TABLE IF NOT EXISTS client_status_catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                status_label TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (client_id, status_label),
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_client_status_catalog_client
                ON client_status_catalog(client_id, sort_order, status_label);
            """
        )
        # Additive materials column on profiles (optional sells field).
        cols = {
            str(r["name"])
            for r in conn.execute("PRAGMA table_info(client_profiles)").fetchall()
        }
        if "materials" not in cols:
            conn.execute(
                "ALTER TABLE client_profiles ADD COLUMN materials TEXT NOT NULL DEFAULT ''"
            )
        if owns:
            conn.commit()
    finally:
        if owns and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _empty_payload() -> dict[str, Any]:
    return {
        "basics": {
            "client_name": "",
            "client_code": "",
            "website": "",
            "main_location": "",
            "description": "",
        },
        "sells": {
            "primary_service": "",
            "secondary_services": "",
            "products_services": "",
            "industries_served": "",
            "equipment_capacity": "",
            "materials": "",
            "certifications": "",
            "differentiators": "",
        },
        "opportunities": {
            "target_customer_types": "",
            "target_industries": "",
            "target_products": "",
            "manufacturing_processes_sought": "",
            "production_preference": "",
            "geographic_preferences": "",
            "positive_signals": "",
            "negative_signals": "",
            "exclusions": "",
            "target_titles": "",
            "fit_weighting_notes": "",
        },
        "crm": {
            "statuses": [],
            "default_status": "",
            "campaign_name": "Default",
            "campaign_description": "",
            "apply_assignments": False,
            "assignment_user_ids": [],
        },
        "overwrite_fields": [],
        "field_tiers": field_tier_map(),
    }


def field_tier_map() -> dict[str, str]:
    tiers: dict[str, str] = {}
    for key in REQUIRED_BASICS:
        tiers[f"basics.{key}"] = "required"
    for key in RECOMMENDED_BASICS:
        tiers[f"basics.{key}"] = "recommended"
    for key in OPTIONAL_BASICS:
        tiers[f"basics.{key}"] = "optional"
    for key in REQUIRED_SELLS:
        tiers[f"sells.{key}"] = "required"
    for key in RECOMMENDED_SELLS:
        tiers[f"sells.{key}"] = "recommended"
    for key in OPTIONAL_SELLS:
        tiers[f"sells.{key}"] = "optional"
    for key in REQUIRED_OPPS:
        tiers[f"opportunities.{key}"] = "required"
    for key in RECOMMENDED_OPPS:
        tiers[f"opportunities.{key}"] = "recommended"
    for key in OPTIONAL_OPPS:
        tiers[f"opportunities.{key}"] = "optional"
    for key in REQUIRED_CRM:
        tiers[f"crm.{key}"] = "required"
    for key in RECOMMENDED_CRM:
        tiers[f"crm.{key}"] = "recommended"
    for key in OPTIONAL_CRM:
        tiers[f"crm.{key}"] = "optional"
    return tiers


def _section_value(section: dict[str, Any], key: str) -> Any:
    if key == "statuses":
        raw = section.get("statuses") or []
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        return []
    if key == "assignment_user_ids":
        raw = section.get("assignment_user_ids") or []
        out: list[int] = []
        for x in raw if isinstance(raw, list) else []:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out
    if key == "apply_assignments":
        return bool(section.get("apply_assignments"))
    return _blank(section.get(key))


def _field_filled(section: dict[str, Any], key: str) -> bool:
    val = _section_value(section, key)
    if isinstance(val, list):
        return len(val) > 0
    if isinstance(val, bool):
        return True
    return bool(_blank(val))


def compute_draft_completion(payload: dict[str, Any]) -> dict[str, Any]:
    scored: list[tuple[str, str, bool]] = []
    sections = {
        "basics": (REQUIRED_BASICS, RECOMMENDED_BASICS, payload.get("basics") or {}),
        "sells": (REQUIRED_SELLS, RECOMMENDED_SELLS, payload.get("sells") or {}),
        "opportunities": (
            REQUIRED_OPPS,
            RECOMMENDED_OPPS,
            payload.get("opportunities") or {},
        ),
        "crm": (REQUIRED_CRM, RECOMMENDED_CRM, payload.get("crm") or {}),
    }
    missing_required: list[str] = []
    missing_recommended: list[str] = []
    for section_name, (req, rec, data) in sections.items():
        data = data if isinstance(data, dict) else {}
        for key in req:
            filled = _field_filled(data, key)
            scored.append((f"{section_name}.{key}", "required", filled))
            if not filled:
                missing_required.append(f"{section_name}.{key}")
        for key in rec:
            filled = _field_filled(data, key)
            scored.append((f"{section_name}.{key}", "recommended", filled))
            if not filled:
                missing_recommended.append(f"{section_name}.{key}")
    # Required counts double weight
    total_w = 0
    filled_w = 0
    for _path, tier, filled in scored:
        w = 2 if tier == "required" else 1
        total_w += w
        if filled:
            filled_w += w
    percent = int(round((filled_w / total_w) * 100)) if total_w else 0
    can_finish = len(missing_required) == 0
    return {
        "percent": percent,
        "missing_required": missing_required,
        "missing_recommended": missing_recommended,
        "can_finish": can_finish,
    }


def _parse_payload(raw: str | None) -> dict[str, Any]:
    base = _empty_payload()
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return base
    if not isinstance(data, dict):
        return base
    for section in ("basics", "sells", "opportunities", "crm"):
        incoming = data.get(section)
        if isinstance(incoming, dict):
            base[section].update(incoming)
    ow = data.get("overwrite_fields")
    if isinstance(ow, list):
        base["overwrite_fields"] = [str(x) for x in ow if str(x).strip()]
    return base


def _require_admin(user: NorthStarUser) -> None:
    if not bool(getattr(user, "is_administrator", False)):
        raise PermissionError("Administrator access required.")
    if not bool(getattr(user, "active", True)):
        raise PermissionError("Administrator access required.")


def _draft_row_to_view(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    payload = _parse_payload(d.get("payload_json"))
    completion = compute_draft_completion(payload)
    return {
        "draft_id": int(d["id"]),
        "client_id": int(d["client_id"]) if d.get("client_id") is not None else None,
        "mode": _blank(d.get("mode")) or "new",
        "status": _blank(d.get("status")) or "draft",
        "current_step": int(d.get("current_step") or 1),
        "payload": payload,
        "template_id": _blank(d.get("template_id")),
        "copy_source_client_id": (
            int(d["copy_source_client_id"])
            if d.get("copy_source_client_id") is not None
            else None
        ),
        "completion_percent": int(completion["percent"]),
        "missing_required": list(completion["missing_required"]),
        "missing_recommended": list(completion["missing_recommended"]),
        "can_finish": bool(completion["can_finish"]),
        "created_at": _blank(d.get("created_at")),
        "updated_at": _blank(d.get("updated_at")),
        "finished_at": _blank(d.get("finished_at")),
        "forbidden_copy_categories": list(FORBIDDEN_COPY_CATEGORIES),
    }


def create_draft(
    *,
    user: NorthStarUser,
    client_id: int | None = None,
    template_id: str | None = None,
) -> dict[str, Any]:
    _require_admin(user)
    ensure_client_onboarding_schema()
    mode = "existing" if client_id is not None else "new"
    payload = _empty_payload()
    copy_source = None
    tid = _blank(template_id)

    with get_connection() as conn:
        if client_id is not None:
            require_write_client_id(int(client_id), user_id=user.id)
            if not user_can_access_client(user.id, int(client_id)):
                raise PermissionError("Not authorized for that client.")
            row = conn.execute(
                "SELECT id, code, name FROM clients WHERE id = ?", (int(client_id),)
            ).fetchone()
            if row is None:
                raise LookupError("Client not found.")
            # Resume: seed from existing setup without inventing blanks over data.
            setup = get_client_setup(int(client_id), user_id=user.id)
            payload["basics"].update(
                {
                    "client_name": setup.client_name,
                    "client_code": setup.client_code,
                    "website": setup.website,
                    "main_location": setup.main_location,
                    "description": setup.description,
                }
            )
            payload["sells"].update(
                {
                    "primary_service": setup.primary_service,
                    "secondary_services": setup.secondary_services,
                    "products_services": setup.products_services,
                    "equipment_capacity": setup.equipment_capacity,
                    "certifications": setup.certifications,
                    "differentiators": setup.differentiators,
                    "materials": getattr(setup, "materials", "") or "",
                }
            )
            camp = next((c for c in setup.campaigns if c.is_default), None)
            if camp is None and setup.campaigns:
                camp = setup.campaigns[0]
            if camp is not None:
                payload["opportunities"].update(
                    {
                        "target_customer_types": camp.target_customer_types,
                        "target_industries": camp.target_industries,
                        "target_products": camp.target_products,
                        "manufacturing_processes_sought": camp.manufacturing_processes_sought,
                        "production_preference": camp.production_preference,
                        "geographic_preferences": camp.geographic_preferences,
                        "positive_signals": camp.positive_signals,
                        "negative_signals": camp.negative_signals,
                        "exclusions": camp.exclusions,
                        "target_titles": camp.target_titles,
                        "fit_weighting_notes": camp.fit_weighting_notes,
                    }
                )
                payload["crm"]["campaign_name"] = camp.campaign_name or "Default"
                payload["crm"]["campaign_description"] = camp.description or ""
            # Seed industries_served from campaign target industries when present.
            if camp and camp.target_industries:
                payload["sells"]["industries_served"] = camp.target_industries
            statuses = [
                _blank(r["status_label"])
                for r in conn.execute(
                    """
                    SELECT status_label FROM client_status_catalog
                    WHERE client_id = ? AND active = 1
                    ORDER BY sort_order, status_label COLLATE NOCASE
                    """,
                    (int(client_id),),
                ).fetchall()
            ]
            if statuses:
                payload["crm"]["statuses"] = statuses
                default = conn.execute(
                    """
                    SELECT status_label FROM client_status_catalog
                    WHERE client_id = ? AND is_default = 1 AND active = 1
                    LIMIT 1
                    """,
                    (int(client_id),),
                ).fetchone()
                payload["crm"]["default_status"] = (
                    _blank(default["status_label"]) if default else statuses[0]
                )

            existing = conn.execute(
                """
                SELECT id FROM client_onboarding_drafts
                WHERE client_id = ? AND status = 'draft'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (int(client_id),),
            ).fetchone()
            if existing:
                return get_draft(int(existing["id"]), user=user)

        if tid:
            tmpl = get_template(tid)
            if tmpl is None:
                raise ValueError("Unknown template.")
            _apply_template_dict(payload, tmpl, overwrite=True)

        completion = compute_draft_completion(payload)
        conn.execute(
            """
            INSERT INTO client_onboarding_drafts (
                client_id, mode, status, current_step, payload_json, template_id,
                copy_source_client_id, completion_percent,
                created_by_user_id, updated_by_user_id, created_at, updated_at
            ) VALUES (?, ?, 'draft', 1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(client_id) if client_id is not None else None,
                mode,
                json.dumps(payload),
                tid,
                copy_source,
                int(completion["percent"]),
                user.id,
                user.id,
                _now(),
                _now(),
            ),
        )
        draft_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
    return get_draft(draft_id, user=user)


def get_draft(draft_id: int, *, user: NorthStarUser) -> dict[str, Any]:
    _require_admin(user)
    ensure_client_onboarding_schema()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM client_onboarding_drafts WHERE id = ?", (int(draft_id),)
        ).fetchone()
        if row is None:
            raise LookupError("Draft not found.")
        if row["client_id"] is not None:
            if not user_can_access_client(user.id, int(row["client_id"])):
                raise PermissionError("Not authorized for that client.")
        return _draft_row_to_view(row)


def list_drafts(*, user: NorthStarUser) -> list[dict[str, Any]]:
    _require_admin(user)
    ensure_client_onboarding_schema()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT d.*, c.name AS client_name
            FROM client_onboarding_drafts d
            LEFT JOIN clients c ON c.id = d.client_id
            WHERE d.status = 'draft'
            ORDER BY d.updated_at DESC
            LIMIT 100
            """
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        view = _draft_row_to_view(row)
        view["client_name"] = _blank(row["client_name"]) or _blank(
            (_parse_payload(row["payload_json"]).get("basics") or {}).get("client_name")
        )
        out.append(view)
    return out


def save_draft(
    draft_id: int,
    *,
    user: NorthStarUser,
    current_step: int | None = None,
    payload_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _require_admin(user)
    ensure_client_onboarding_schema()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM client_onboarding_drafts WHERE id = ?", (int(draft_id),)
        ).fetchone()
        if row is None:
            raise LookupError("Draft not found.")
        if _blank(row["status"]) != "draft":
            raise ValueError("Draft is no longer editable.")
        if row["client_id"] is not None:
            require_write_client_id(int(row["client_id"]), user_id=user.id)
        payload = _parse_payload(row["payload_json"])
        if isinstance(payload_patch, dict):
            for section in ("basics", "sells", "opportunities", "crm"):
                incoming = payload_patch.get(section)
                if isinstance(incoming, dict):
                    payload[section].update(incoming)
            if isinstance(payload_patch.get("overwrite_fields"), list):
                payload["overwrite_fields"] = [
                    str(x) for x in payload_patch["overwrite_fields"] if str(x).strip()
                ]
        step = int(current_step) if current_step is not None else int(row["current_step"] or 1)
        step = max(1, min(5, step))
        completion = compute_draft_completion(payload)
        conn.execute(
            """
            UPDATE client_onboarding_drafts
            SET current_step = ?, payload_json = ?, completion_percent = ?,
                updated_by_user_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                step,
                json.dumps(payload),
                int(completion["percent"]),
                user.id,
                _now(),
                int(draft_id),
            ),
        )
        conn.commit()
    return get_draft(draft_id, user=user)


def _apply_template_dict(
    payload: dict[str, Any], tmpl: dict[str, Any], *, overwrite: bool
) -> None:
    sells = tmpl.get("sells") if isinstance(tmpl.get("sells"), dict) else {}
    opps = tmpl.get("opportunities") if isinstance(tmpl.get("opportunities"), dict) else {}
    crm = tmpl.get("crm") if isinstance(tmpl.get("crm"), dict) else {}
    for key in COPYABLE_SELLS_FIELDS:
        if key == "industries_served":
            continue
        src = _blank(sells.get(key))
        if not src:
            continue
        if overwrite or not _blank(payload["sells"].get(key)):
            payload["sells"][key] = src
    # industries served often lives on template opportunities.target_industries
    industries = _blank(sells.get("industries_served")) or _blank(opps.get("target_industries"))
    if industries and (overwrite or not _blank(payload["sells"].get("industries_served"))):
        payload["sells"]["industries_served"] = industries
    for key in COPYABLE_OPPORTUNITY_FIELDS:
        src = _blank(opps.get(key))
        if not src:
            continue
        if overwrite or not _blank(payload["opportunities"].get(key)):
            payload["opportunities"][key] = src
    for key in COPYABLE_CRM_FIELDS:
        if key == "statuses":
            statuses = crm.get("statuses") if isinstance(crm.get("statuses"), list) else []
            cleaned = [str(s).strip() for s in statuses if str(s).strip()]
            if cleaned and (overwrite or not payload["crm"].get("statuses")):
                payload["crm"]["statuses"] = cleaned
            continue
        src = _blank(crm.get(key)) if key != "campaign_description" else _blank(
            crm.get("campaign_description") or crm.get("description")
        )
        if key == "campaign_description":
            dest_key = "campaign_description"
        else:
            dest_key = key
        if not src:
            continue
        if overwrite or not _blank(payload["crm"].get(dest_key)):
            payload["crm"][dest_key] = src


def apply_template_to_draft(
    draft_id: int,
    *,
    user: NorthStarUser,
    template_id: str,
    overwrite_populated: bool = False,
) -> dict[str, Any]:
    _require_admin(user)
    tmpl = get_template(template_id)
    if tmpl is None:
        raise ValueError("Unknown template.")
    draft = get_draft(draft_id, user=user)
    if draft["status"] != "draft":
        raise ValueError("Draft is no longer editable.")
    payload = draft["payload"]
    _apply_template_dict(payload, tmpl, overwrite=overwrite_populated)
    with get_connection() as conn:
        completion = compute_draft_completion(payload)
        conn.execute(
            """
            UPDATE client_onboarding_drafts
            SET payload_json = ?, template_id = ?, completion_percent = ?,
                updated_by_user_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(payload),
                _blank(template_id),
                int(completion["percent"]),
                user.id,
                _now(),
                int(draft_id),
            ),
        )
        # Audit metadata on destination client when known
        if draft["client_id"] is not None:
            _audit(
                conn,
                client_id=int(draft["client_id"]),
                campaign_id=None,
                entity_type="onboarding_draft",
                field_name="template_applied",
                old_value="",
                new_value=_blank(template_id),
                user_id=user.id,
                user_name=_blank(user.full_name) or _blank(user.email),
                change_source=f"{TEMPLATE_SOURCE_PREFIX}:{_blank(template_id)}",
            )
        conn.commit()
    return get_draft(draft_id, user=user)


def _source_setup_bundle(conn: sqlite3.Connection, source_client_id: int) -> dict[str, Any]:
    client = conn.execute(
        "SELECT id, code, name FROM clients WHERE id = ?", (source_client_id,)
    ).fetchone()
    if client is None:
        raise LookupError("Source client not found.")
    ensure_client_setup_schema(conn)
    _ensure_profile_row(conn, source_client_id)
    profile = dict(
        conn.execute(
            "SELECT * FROM client_profiles WHERE client_id = ?", (source_client_id,)
        ).fetchone()
    )
    camp = conn.execute(
        """
        SELECT * FROM client_campaigns
        WHERE client_id = ? AND is_default = 1
        ORDER BY id LIMIT 1
        """,
        (source_client_id,),
    ).fetchone()
    if camp is None:
        camp = conn.execute(
            "SELECT * FROM client_campaigns WHERE client_id = ? ORDER BY id LIMIT 1",
            (source_client_id,),
        ).fetchone()
    camp_d = dict(camp) if camp else {}
    statuses = [
        _blank(r["status_label"])
        for r in conn.execute(
            """
            SELECT status_label FROM client_status_catalog
            WHERE client_id = ? AND active = 1
            ORDER BY sort_order, status_label COLLATE NOCASE
            """,
            (source_client_id,),
        ).fetchall()
    ]
    default_status = ""
    drow = conn.execute(
        """
        SELECT status_label FROM client_status_catalog
        WHERE client_id = ? AND is_default = 1 AND active = 1 LIMIT 1
        """,
        (source_client_id,),
    ).fetchone()
    if drow:
        default_status = _blank(drow["status_label"])
    return {
        "client_id": int(client["id"]),
        "client_name": _blank(client["name"]),
        "sells": {k: _blank(profile.get(k)) for k in COPYABLE_SELLS_FIELDS},
        "opportunities": {k: _blank(camp_d.get(k)) for k in COPYABLE_OPPORTUNITY_FIELDS},
        "crm": {
            "statuses": statuses,
            "default_status": default_status or (statuses[0] if statuses else ""),
            "campaign_name": _blank(camp_d.get("campaign_name")) or "Default",
            "campaign_description": _blank(camp_d.get("description")),
        },
    }


def preview_copy_from_client(
    *,
    user: NorthStarUser,
    source_client_id: int,
    draft_id: int | None = None,
    destination_client_id: int | None = None,
) -> dict[str, Any]:
    """Preview exactly which capability/status/campaign fields would copy."""
    _require_admin(user)
    ensure_client_onboarding_schema()
    if not user_can_access_client(user.id, int(source_client_id)):
        raise PermissionError("Not authorized for source client.")
    dest_payload = _empty_payload()
    dest_client_id = destination_client_id
    with get_connection() as conn:
        source = _source_setup_bundle(conn, int(source_client_id))
        if draft_id is not None:
            drow = conn.execute(
                "SELECT * FROM client_onboarding_drafts WHERE id = ?", (int(draft_id),)
            ).fetchone()
            if drow is None:
                raise LookupError("Draft not found.")
            dest_payload = _parse_payload(drow["payload_json"])
            if drow["client_id"] is not None:
                dest_client_id = int(drow["client_id"])
        elif destination_client_id is not None:
            if not user_can_access_client(user.id, int(destination_client_id)):
                raise PermissionError("Not authorized for destination client.")
            # Build payload from existing destination for conflict detection
            setup = get_client_setup(int(destination_client_id), user_id=user.id)
            dest_payload["sells"].update(
                {
                    "primary_service": setup.primary_service,
                    "secondary_services": setup.secondary_services,
                    "products_services": setup.products_services,
                    "equipment_capacity": setup.equipment_capacity,
                    "certifications": setup.certifications,
                }
            )

    fields: list[dict[str, Any]] = []
    conflicts: list[str] = []

    def add_field(path: str, src: Any, dest: Any) -> None:
        src_s = src if isinstance(src, list) else _blank(src)
        dest_filled = (
            len(dest) > 0 if isinstance(dest, list) else bool(_blank(dest))
        )
        src_filled = len(src_s) > 0 if isinstance(src_s, list) else bool(_blank(src_s))
        will_copy = bool(src_filled) and (not dest_filled)
        blocked = ""
        if src_filled and dest_filled:
            blocked = "Destination already populated — requires overwrite confirmation"
            conflicts.append(path)
            will_copy = False
        if not src_filled:
            blocked = "Source empty"
            will_copy = False
        fields.append(
            {
                "path": path,
                "source_value": src_s,
                "destination_value": dest if isinstance(dest, list) else _blank(dest),
                "will_copy": will_copy,
                "blocked_reason": blocked,
            }
        )

    for key in COPYABLE_SELLS_FIELDS:
        add_field(f"sells.{key}", source["sells"].get(key), dest_payload["sells"].get(key))
    for key in COPYABLE_OPPORTUNITY_FIELDS:
        add_field(
            f"opportunities.{key}",
            source["opportunities"].get(key),
            dest_payload["opportunities"].get(key),
        )
    for key in COPYABLE_CRM_FIELDS:
        add_field(f"crm.{key}", source["crm"].get(key), dest_payload["crm"].get(key))

    return {
        "source_client_id": int(source_client_id),
        "source_client_name": source["client_name"],
        "destination_client_id": dest_client_id,
        "draft_id": draft_id,
        "fields": fields,
        "conflict_paths": conflicts,
        "never_copied": list(FORBIDDEN_COPY_CATEGORIES),
        "copyable_only": (
            list(COPYABLE_SELLS_FIELDS)
            + list(COPYABLE_OPPORTUNITY_FIELDS)
            + list(COPYABLE_CRM_FIELDS)
        ),
    }


def apply_copy_to_draft(
    draft_id: int,
    *,
    user: NorthStarUser,
    source_client_id: int,
    overwrite_fields: list[str] | None = None,
) -> dict[str, Any]:
    _require_admin(user)
    preview = preview_copy_from_client(
        user=user, source_client_id=source_client_id, draft_id=draft_id
    )
    overwrite = {str(x) for x in (overwrite_fields or []) if str(x).strip()}
    draft = get_draft(draft_id, user=user)
    payload = draft["payload"]
    for field in preview["fields"]:
        path = field["path"]
        if field["will_copy"] or path in overwrite:
            if not field["source_value"] and path not in overwrite:
                continue
            if field["destination_value"] and path not in overwrite and not field["will_copy"]:
                continue
            section, key = path.split(".", 1)
            payload[section][key] = field["source_value"]
    with get_connection() as conn:
        completion = compute_draft_completion(payload)
        conn.execute(
            """
            UPDATE client_onboarding_drafts
            SET payload_json = ?, copy_source_client_id = ?, completion_percent = ?,
                updated_by_user_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(payload),
                int(source_client_id),
                int(completion["percent"]),
                user.id,
                _now(),
                int(draft_id),
            ),
        )
        if draft["client_id"] is not None:
            _audit(
                conn,
                client_id=int(draft["client_id"]),
                campaign_id=None,
                entity_type="onboarding_draft",
                field_name="copy_applied",
                old_value="",
                new_value=str(int(source_client_id)),
                user_id=user.id,
                user_name=_blank(user.full_name) or _blank(user.email),
                change_source=f"{COPY_SOURCE_PREFIX}:{int(source_client_id)}",
            )
        conn.commit()
    return get_draft(draft_id, user=user)


def _unique_client_code(conn: sqlite3.Connection, desired: str) -> str:
    base = _slug_code(desired)
    code = base
    n = 2
    while conn.execute("SELECT 1 FROM clients WHERE code = ?", (code,)).fetchone():
        code = f"{base}-{n}"[:48]
        n += 1
    return code


def _write_if_allowed(
    *,
    dest: str,
    incoming: str,
    overwrite_fields: set[str],
    path: str,
) -> str | None:
    """Return new value to write, or None to skip (preserve destination)."""
    inc = _blank(incoming)
    cur = _blank(dest)
    if not inc:
        return None  # blank draft never erases
    if not cur or cur == inc or path in overwrite_fields:
        return inc
    return None  # populated dest, no overwrite confirmation


def finish_draft(draft_id: int, *, user: NorthStarUser) -> dict[str, Any]:
    """Apply draft transactionally to client profile + default campaign + status catalog."""
    _require_admin(user)
    ensure_client_onboarding_schema()
    draft = get_draft(draft_id, user=user)
    if draft["status"] != "draft":
        raise ValueError("Draft already finished.")
    completion = compute_draft_completion(draft["payload"])
    if not completion["can_finish"]:
        raise ValueError(
            "Required fields missing: " + ", ".join(completion["missing_required"])
        )

    payload = draft["payload"]
    basics = payload["basics"]
    sells = payload["sells"]
    opps = payload["opportunities"]
    crm = payload["crm"]
    overwrite = {str(x) for x in (payload.get("overwrite_fields") or [])}

    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            client_id = draft["client_id"]
            created_new = False
            if client_id is None:
                name = _blank(basics.get("client_name"))
                if not name:
                    raise ValueError("Client name is required.")
                code = _blank(basics.get("client_code")) or _unique_client_code(conn, name)
                if conn.execute("SELECT 1 FROM clients WHERE code = ?", (code,)).fetchone():
                    code = _unique_client_code(conn, code)
                conn.execute(
                    "INSERT INTO clients (code, name) VALUES (?, ?)",
                    (code, name),
                )
                client_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                created_new = True
                _audit(
                    conn,
                    client_id=client_id,
                    campaign_id=None,
                    entity_type="client",
                    field_name="created",
                    old_value="",
                    new_value=name,
                    user_id=user.id,
                    user_name=_blank(user.full_name) or _blank(user.email),
                    change_source=ONBOARDING_SOURCE,
                )
            else:
                client_id = int(client_id)
                require_write_client_id(client_id, user_id=user.id)
                # Rename only when provided and confirmed/empty conflict rules
                row = conn.execute(
                    "SELECT name FROM clients WHERE id = ?", (client_id,)
                ).fetchone()
                if row is None:
                    raise LookupError("Client not found.")
                new_name = _blank(basics.get("client_name"))
                if new_name and new_name != _blank(row["name"]):
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
                        old_value=_blank(row["name"]),
                        new_value=new_name,
                        user_id=user.id,
                        user_name=_blank(user.full_name) or _blank(user.email),
                        change_source=ONBOARDING_SOURCE,
                    )

            _ensure_profile_row(conn, client_id)
            profile = dict(
                conn.execute(
                    "SELECT * FROM client_profiles WHERE client_id = ?", (client_id,)
                ).fetchone()
            )
            profile_updates: dict[str, Any] = {}
            profile_sources: list[tuple[str, str, dict[str, Any]]] = [
                ("website", "basics.website", basics),
                ("main_location", "basics.main_location", basics),
                ("description", "basics.description", basics),
                ("primary_service", "sells.primary_service", sells),
                ("secondary_services", "sells.secondary_services", sells),
                ("products_services", "sells.products_services", sells),
                ("equipment_capacity", "sells.equipment_capacity", sells),
                ("materials", "sells.materials", sells),
                ("certifications", "sells.certifications", sells),
                ("differentiators", "sells.differentiators", sells),
            ]
            for key, path, section in profile_sources:
                field_key = path.split(".", 1)[1]
                # Skip materials when column not present on older DBs mid-txn.
                if key == "materials" and "materials" not in profile:
                    continue
                nxt = _write_if_allowed(
                    dest=_blank(profile.get(key)),
                    incoming=_blank(section.get(field_key)),
                    overwrite_fields=overwrite,
                    path=path,
                )
                if nxt is not None and nxt != _blank(profile.get(key)):
                    profile_updates[key] = nxt
                    _audit(
                        conn,
                        client_id=client_id,
                        campaign_id=None,
                        entity_type="client_profile",
                        field_name=key,
                        old_value=_blank(profile.get(key)),
                        new_value=nxt,
                        user_id=user.id,
                        user_name=_blank(user.full_name) or _blank(user.email),
                        change_source=ONBOARDING_SOURCE,
                    )
            if profile_updates:
                sets = ", ".join(f"{k} = ?" for k in profile_updates)
                vals = list(profile_updates.values()) + [_now(), client_id]
                conn.execute(
                    f"UPDATE client_profiles SET {sets}, updated_at = ? WHERE client_id = ?",
                    vals,
                )

            # Default campaign
            camp = conn.execute(
                """
                SELECT * FROM client_campaigns
                WHERE client_id = ? AND is_default = 1 ORDER BY id LIMIT 1
                """,
                (client_id,),
            ).fetchone()
            if camp is None:
                camp = conn.execute(
                    "SELECT * FROM client_campaigns WHERE client_id = ? ORDER BY id LIMIT 1",
                    (client_id,),
                ).fetchone()
            camp_name = _blank(crm.get("campaign_name")) or "Default"
            camp_desc = _blank(crm.get("campaign_description"))
            camp_fields = {
                "campaign_name": camp_name,
                "description": camp_desc,
                "primary_service": _blank(sells.get("primary_service")),
                "secondary_services": _blank(sells.get("secondary_services")),
                "target_industries": _blank(opps.get("target_industries"))
                or _blank(sells.get("industries_served")),
                "target_customer_types": _blank(opps.get("target_customer_types")),
                "target_products": _blank(opps.get("target_products")),
                "manufacturing_processes_sought": _blank(
                    opps.get("manufacturing_processes_sought")
                ),
                "production_preference": _blank(opps.get("production_preference")),
                "geographic_preferences": _blank(opps.get("geographic_preferences")),
                "positive_signals": _blank(opps.get("positive_signals")),
                "negative_signals": _blank(opps.get("negative_signals")),
                "exclusions": _blank(opps.get("exclusions")),
                "target_titles": _blank(opps.get("target_titles")),
                "fit_weighting_notes": _blank(opps.get("fit_weighting_notes")),
            }
            if camp is None:
                conn.execute(
                    """
                    INSERT INTO client_campaigns (
                        client_id, campaign_name, description, is_active, is_default,
                        primary_service, secondary_services, target_industries,
                        target_customer_types, target_products,
                        manufacturing_processes_sought, production_preference,
                        geographic_preferences, positive_signals, negative_signals,
                        exclusions, target_titles, fit_weighting_notes,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        client_id,
                        camp_fields["campaign_name"],
                        camp_fields["description"],
                        camp_fields["primary_service"],
                        camp_fields["secondary_services"],
                        camp_fields["target_industries"],
                        camp_fields["target_customer_types"],
                        camp_fields["target_products"],
                        camp_fields["manufacturing_processes_sought"],
                        camp_fields["production_preference"],
                        camp_fields["geographic_preferences"],
                        camp_fields["positive_signals"],
                        camp_fields["negative_signals"],
                        camp_fields["exclusions"],
                        camp_fields["target_titles"],
                        camp_fields["fit_weighting_notes"],
                        _now(),
                        _now(),
                    ),
                )
                campaign_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.execute(
                    "UPDATE client_profiles SET default_campaign_id = ? WHERE client_id = ?",
                    (campaign_id, client_id),
                )
                _audit(
                    conn,
                    client_id=client_id,
                    campaign_id=campaign_id,
                    entity_type="client_campaign",
                    field_name="created",
                    old_value="",
                    new_value=camp_fields["campaign_name"],
                    user_id=user.id,
                    user_name=_blank(user.full_name) or _blank(user.email),
                    change_source=ONBOARDING_SOURCE,
                )
            else:
                campaign_id = int(camp["id"])
                camp_d = dict(camp)
                updates: dict[str, Any] = {}
                path_map = {
                    "campaign_name": "crm.campaign_name",
                    "description": "crm.campaign_description",
                    "primary_service": "sells.primary_service",
                    "secondary_services": "sells.secondary_services",
                    "target_industries": "opportunities.target_industries",
                    "target_customer_types": "opportunities.target_customer_types",
                    "target_products": "opportunities.target_products",
                    "manufacturing_processes_sought": "opportunities.manufacturing_processes_sought",
                    "production_preference": "opportunities.production_preference",
                    "geographic_preferences": "opportunities.geographic_preferences",
                    "positive_signals": "opportunities.positive_signals",
                    "negative_signals": "opportunities.negative_signals",
                    "exclusions": "opportunities.exclusions",
                    "target_titles": "opportunities.target_titles",
                    "fit_weighting_notes": "opportunities.fit_weighting_notes",
                }
                for col, path in path_map.items():
                    nxt = _write_if_allowed(
                        dest=_blank(camp_d.get(col)),
                        incoming=_blank(camp_fields.get(col)),
                        overwrite_fields=overwrite,
                        path=path,
                    )
                    if nxt is not None and nxt != _blank(camp_d.get(col)):
                        updates[col] = nxt
                        _audit(
                            conn,
                            client_id=client_id,
                            campaign_id=campaign_id,
                            entity_type="client_campaign",
                            field_name=col,
                            old_value=_blank(camp_d.get(col)),
                            new_value=nxt,
                            user_id=user.id,
                            user_name=_blank(user.full_name) or _blank(user.email),
                            change_source=ONBOARDING_SOURCE,
                        )
                if updates:
                    sets = ", ".join(f"{k} = ?" for k in updates)
                    vals = list(updates.values()) + [_now(), campaign_id, client_id]
                    conn.execute(
                        f"""
                        UPDATE client_campaigns SET {sets}, updated_at = ?
                        WHERE id = ? AND client_id = ?
                        """,
                        vals,
                    )

            # Status catalog (labels only — never CCR rows)
            statuses = [
                str(s).strip()
                for s in (crm.get("statuses") or [])
                if str(s).strip()
            ]
            default_status = _blank(crm.get("default_status"))
            if default_status and default_status not in statuses:
                statuses.append(default_status)
            for idx, label in enumerate(statuses):
                conn.execute(
                    """
                    INSERT INTO client_status_catalog (
                        client_id, status_label, is_default, sort_order, active,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(client_id, status_label) DO UPDATE SET
                        is_default = excluded.is_default,
                        sort_order = excluded.sort_order,
                        active = 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        client_id,
                        label,
                        1 if label == default_status else 0,
                        idx,
                        _now(),
                        _now(),
                    ),
                )
            if default_status:
                conn.execute(
                    """
                    UPDATE client_status_catalog
                    SET is_default = CASE WHEN status_label = ? THEN 1 ELSE 0 END,
                        updated_at = ?
                    WHERE client_id = ?
                    """,
                    (default_status, _now(), client_id),
                )

            # Assignments only when explicitly selected
            if bool(crm.get("apply_assignments")):
                for uid in crm.get("assignment_user_ids") or []:
                    try:
                        uid_i = int(uid)
                    except (TypeError, ValueError):
                        continue
                    urow = conn.execute(
                        "SELECT id FROM users WHERE id = ? AND active = 1", (uid_i,)
                    ).fetchone()
                    if urow is None:
                        continue
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO user_client_assignments (
                            user_id, client_id, role, active, assigned_at
                        ) VALUES (?, ?, 'account_executive', 1, datetime('now'))
                        """,
                        (uid_i, client_id),
                    )

            # Record template/copy sources
            if draft.get("template_id"):
                _audit(
                    conn,
                    client_id=client_id,
                    campaign_id=None,
                    entity_type="onboarding",
                    field_name="template_id",
                    old_value="",
                    new_value=_blank(draft.get("template_id")),
                    user_id=user.id,
                    user_name=_blank(user.full_name) or _blank(user.email),
                    change_source=f"{TEMPLATE_SOURCE_PREFIX}:{_blank(draft.get('template_id'))}",
                )
            if draft.get("copy_source_client_id") is not None:
                _audit(
                    conn,
                    client_id=client_id,
                    campaign_id=None,
                    entity_type="onboarding",
                    field_name="copy_source_client_id",
                    old_value="",
                    new_value=str(int(draft["copy_source_client_id"])),
                    user_id=user.id,
                    user_name=_blank(user.full_name) or _blank(user.email),
                    change_source=f"{COPY_SOURCE_PREFIX}:{int(draft['copy_source_client_id'])}",
                )

            conn.execute(
                """
                UPDATE client_onboarding_drafts
                SET client_id = ?, status = 'finished', finished_at = ?,
                    completion_percent = 100, updated_by_user_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (client_id, _now(), user.id, _now(), int(draft_id)),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    setup = get_client_setup(int(client_id), user_id=user.id)
    return {
        "draft_id": int(draft_id),
        "client_id": int(client_id),
        "created_new_client": created_new,
        "status": "finished",
        "client_name": setup.client_name,
        "client_code": setup.client_code,
        "setup_href": f"/clients/{int(client_id)}/setup",
        "completion": setup.completeness.model_dump()
        if hasattr(setup.completeness, "model_dump")
        else dict(setup.completeness),
    }


def onboarding_templates_payload() -> dict[str, Any]:
    return {
        "templates": list_templates(),
        "forbidden_copy_categories": list(FORBIDDEN_COPY_CATEGORIES),
        "field_tiers": field_tier_map(),
        "zoominfo_boundary_doc": "docs/client_onboarding_zoominfo_boundary.md",
    }
