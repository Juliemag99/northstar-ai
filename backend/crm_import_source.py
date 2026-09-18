"""CRM import source-type constants and helpers.

Stable values match data_steward provenance:
  LEADMASTER
  CRM_IMPORT

Default is CRM_IMPORT (conservative). Never infer LeadMaster from a column name.
"""

from __future__ import annotations

from data_steward import SOURCE_CRM_IMPORT, SOURCE_LEADMASTER

SOURCE_TYPE_LEADMASTER = SOURCE_LEADMASTER
SOURCE_TYPE_CRM_IMPORT = SOURCE_CRM_IMPORT
DEFAULT_SOURCE_TYPE = SOURCE_TYPE_CRM_IMPORT

VALID_SOURCE_TYPES = frozenset({SOURCE_TYPE_LEADMASTER, SOURCE_TYPE_CRM_IMPORT})

UNQUOTED_NEWLINE_DETAIL = (
    "This CSV appears to contain line breaks inside unquoted fields. "
    "Please correct the file or upload XLSX."
)
SOURCE_TYPE_INVALID = "Choose LeadMaster or Other / Generic CRM Import."
IDENTITY_BOUND_DETAIL = (
    "LeadMaster Record No. is already bound to a different company. "
    "Use the existing company or skip this row."
)


def blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_source_type(value: object | None) -> str:
    text = blank(value).upper().replace("-", "_").replace(" ", "_")
    if text in {"LEADMASTER", "LM", "SOURCE_LEADMASTER"}:
        return SOURCE_TYPE_LEADMASTER
    if text in {"CRM_IMPORT", "CRMIMPORT", "OTHER", "GENERIC", "GENERIC_CRM"}:
        return SOURCE_TYPE_CRM_IMPORT
    if not text:
        return DEFAULT_SOURCE_TYPE
    raise ValueError(SOURCE_TYPE_INVALID)


def is_leadmaster_source(value: object | None) -> bool:
    try:
        return normalize_source_type(value) == SOURCE_TYPE_LEADMASTER
    except ValueError:
        return False


def alias_source_system(source_type: str) -> str:
    """company_aliases.source_system for this import.

    LeadMaster writes LEADMASTER (same as P3/P4B). Generic CRM writes crm_import.
    """
    if is_leadmaster_source(source_type):
        return SOURCE_TYPE_LEADMASTER
    from company_aliases import SOURCE_CRM_IMPORT as ALIAS_CRM

    return ALIAS_CRM
