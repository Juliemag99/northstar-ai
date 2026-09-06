"""Maintainable client onboarding templates (JSON-backed, not UI-hardcoded)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_TEMPLATES_PATH = Path(__file__).resolve().parent / "client_onboarding_templates.json"

# Fields that may be copied from a template or another client into a draft/destination.
COPYABLE_SELLS_FIELDS = (
    "primary_service",
    "secondary_services",
    "products_services",
    "equipment_capacity",
    "materials",
    "certifications",
    "differentiators",
    "value_proposition",
)

COPYABLE_OPPORTUNITY_FIELDS = (
    "target_customer_types",
    "target_industries",
    "target_products",
    "manufacturing_processes_sought",
    "production_preference",
    "geographic_preferences",
    "geography_mode",
    "positive_signals",
    "negative_signals",
    "exclusions",
    "target_titles",
    "fit_weighting_notes",
    "company_size_preferences",
    "tooling_notes",
    "stamping_capability",
)

COPYABLE_CRM_FIELDS = (
    "statuses",
    "default_status",
    "campaign_name",
    "campaign_description",
)

# Never copy these categories (enforced in preview/apply).
FORBIDDEN_COPY_CATEGORIES = (
    "prospects",
    "companies",
    "contacts",
    "notes",
    "activities",
    "research_results",
    "api_credentials",
    "email_connections",
    "users",
    "assignments",
    "historical_audit",
    "client_identity",
)


@lru_cache(maxsize=1)
def load_template_catalog() -> dict[str, Any]:
    raw = json.loads(_TEMPLATES_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "templates" not in raw:
        raise RuntimeError("Invalid onboarding templates file.")
    return raw


def list_templates() -> list[dict[str, Any]]:
    catalog = load_template_catalog()
    out: list[dict[str, Any]] = []
    for row in catalog.get("templates") or []:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("id") or "").strip()
        if not tid:
            continue
        out.append(
            {
                "id": tid,
                "name": str(row.get("name") or tid),
                "description": str(row.get("description") or ""),
            }
        )
    return out


def get_template(template_id: str) -> dict[str, Any] | None:
    tid = (template_id or "").strip()
    if not tid:
        return None
    for row in load_template_catalog().get("templates") or []:
        if isinstance(row, dict) and str(row.get("id") or "").strip() == tid:
            return dict(row)
    return None
