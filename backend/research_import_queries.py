"""Small read helpers so Ask NorthStar can later query research attributes."""
from __future__ import annotations

from typing import Any

from import_brown_industries import norm_name
from research_import_mapping import blank
from research_import_schema import ensure_research_import_schema, is_production_db


def query_research_prospects(
    conn,
    client_id: int,
    *,
    priority_code: str = "",
    target_market: str = "",
    potential_component: str = "",
    target_department: str = "",
    batch_id: int | None = None,
    company_id: int | None = None,
) -> list[dict[str, Any]]:
    if is_production_db(conn):
        return []
    ensure_research_import_schema(conn)
    sql = """
        SELECT r.id, r.client_id, r.company_id, r.batch_id,
               r.research_priority_code, r.research_priority_label,
               r.why_client_fits, r.qualification_notes, r.researched_at,
               c.company_name
        FROM client_company_research r
        JOIN companies c ON c.id = r.company_id
        WHERE r.client_id=? AND r.is_current=1
    """
    params: list[Any] = [int(client_id)]
    if blank(priority_code):
        sql += " AND upper(r.research_priority_code)=upper(?)"
        params.append(blank(priority_code))
    if batch_id:
        sql += " AND r.batch_id=?"
        params.append(int(batch_id))
    if company_id:
        sql += " AND r.company_id=?"
        params.append(int(company_id))
    sql += " ORDER BY r.research_priority_code, c.company_name"
    rows = []
    for raw in conn.execute(sql, params):
        rec = {
            "research_id": int(raw["id"]),
            "company_id": int(raw["company_id"]),
            "company_name": blank(raw["company_name"]),
            "batch_id": raw["batch_id"],
            "research_priority_code": blank(raw["research_priority_code"]),
            "research_priority_label": blank(raw["research_priority_label"]),
            "why_client_fits": blank(raw["why_client_fits"]),
            "qualification_notes": blank(raw["qualification_notes"]),
            "researched_at": blank(raw["researched_at"]),
            "attributes": {
                "target_market": [],
                "equipment_product": [],
                "potential_component": [],
                "target_department": [],
            },
        }
        for attr in conn.execute(
            """
            SELECT attribute_type, attribute_value, value_type, numeric_value,
                   date_value, boolean_value, original_value, display_label
            FROM research_attributes
            WHERE research_id=? AND is_current=1
            """,
            (int(raw["id"]),),
        ):
            kind = blank(attr["attribute_type"])
            rec["attributes"].setdefault(kind, []).append(blank(attr["attribute_value"]))
            rec.setdefault("typed_attributes", []).append(
                {
                    "key": kind,
                    "label": blank(attr["display_label"]) if "display_label" in attr.keys() else kind,
                    "value": blank(attr["attribute_value"]),
                    "value_type": blank(attr["value_type"]) if "value_type" in attr.keys() else "TEXT",
                    "numeric_value": attr["numeric_value"] if "numeric_value" in attr.keys() else None,
                    "original_value": blank(attr["original_value"]) if "original_value" in attr.keys() else blank(attr["attribute_value"]),
                }
            )
        rows.append(rec)

    def _has(rec: dict[str, Any], kind: str, needle: str) -> bool:
        want = norm_name(needle)
        return any(want in norm_name(v) or norm_name(v) in want for v in rec["attributes"].get(kind, []))

    if blank(target_market):
        rows = [r for r in rows if _has(r, "target_market", target_market)]
    if blank(potential_component):
        rows = [r for r in rows if _has(r, "potential_component", potential_component)]
    if blank(target_department):
        rows = [r for r in rows if _has(r, "target_department", target_department)]
    return rows


def query_custom_attribute(
    conn,
    client_id: int,
    attribute_key: str,
    *,
    text_value: str = "",
    min_numeric: float | None = None,
    max_numeric: float | None = None,
    source_type: str = "",
    source_label: str = "",
    batch_id: int | None = None,
) -> list[dict[str, Any]]:
    """Future Ask helper: filter prospects by governed custom attributes / source lineage."""
    if is_production_db(conn):
        return []
    ensure_research_import_schema(conn)
    key = blank(attribute_key)
    if not key:
        return []
    sql = """
        SELECT a.company_id, c.company_name, a.attribute_type, a.attribute_value,
               a.value_type, a.numeric_value, a.original_value, a.display_label,
               a.batch_id, b.source_type, b.source_label, b.original_filename
        FROM research_attributes a
        JOIN companies c ON c.id = a.company_id
        LEFT JOIN research_import_batches b ON b.id = a.batch_id
        WHERE a.is_current=1 AND a.attribute_type=?
          AND (a.client_id=? OR a.client_id IS NULL)
    """
    params: list[Any] = [key, int(client_id)]
    if batch_id:
        sql += " AND a.batch_id=?"
        params.append(int(batch_id))
    if blank(source_type):
        sql += " AND b.source_type=?"
        params.append(blank(source_type))
    if blank(source_label):
        sql += " AND lower(b.source_label)=lower(?)"
        params.append(blank(source_label))
    rows = []
    for raw in conn.execute(sql, params):
        numeric = raw["numeric_value"] if "numeric_value" in raw.keys() else None
        original = blank(raw["original_value"]) if "original_value" in raw.keys() else blank(raw["attribute_value"])
        if blank(text_value):
            needle = norm_name(text_value)
            if needle not in norm_name(original) and needle not in norm_name(raw["attribute_value"]):
                continue
        if min_numeric is not None and (numeric is None or float(numeric) < float(min_numeric)):
            continue
        if max_numeric is not None and (numeric is None or float(numeric) > float(max_numeric)):
            continue
        rows.append(
            {
                "company_id": int(raw["company_id"]),
                "company_name": blank(raw["company_name"]),
                "attribute_key": blank(raw["attribute_type"]),
                "attribute_label": blank(raw["display_label"]) if "display_label" in raw.keys() else blank(raw["attribute_type"]),
                "value": blank(raw["attribute_value"]),
                "original_value": original,
                "numeric_value": None if numeric is None else float(numeric),
                "batch_id": raw["batch_id"],
                "source_type": blank(raw["source_type"]) if raw["source_type"] is not None else "",
                "source_label": blank(raw["source_label"]) if raw["source_label"] is not None else "",
                "original_filename": blank(raw["original_filename"]) if raw["original_filename"] is not None else "",
            }
        )
    return rows


def query_research_by_source(
    conn,
    client_id: int,
    *,
    source_type: str = "",
    source_label: str = "",
    supplied_by: str = "",
    batch_id: int | None = None,
) -> list[dict[str, Any]]:
    if is_production_db(conn):
        return []
    ensure_research_import_schema(conn)
    sql = """
        SELECT r.id, r.company_id, c.company_name, r.batch_id,
               b.source_type, b.source_label, b.source_supplied_by,
               b.original_filename, b.sha256, b.mapping_template_name,
               b.is_ai_generated, b.received_at, b.research_method
        FROM client_company_research r
        JOIN companies c ON c.id = r.company_id
        JOIN research_import_batches b ON b.id = r.batch_id
        WHERE r.client_id=? AND r.is_current=1
    """
    params: list[Any] = [int(client_id)]
    if batch_id:
        sql += " AND r.batch_id=?"
        params.append(int(batch_id))
    if blank(source_type):
        sql += " AND b.source_type=?"
        params.append(blank(source_type))
    if blank(source_label):
        sql += " AND lower(b.source_label)=lower(?)"
        params.append(blank(source_label))
    if blank(supplied_by):
        sql += " AND lower(b.source_supplied_by)=lower(?)"
        params.append(blank(supplied_by))
    return [
        {
            "research_id": int(row["id"]),
            "company_id": int(row["company_id"]),
            "company_name": blank(row["company_name"]),
            "batch_id": row["batch_id"],
            "source_type": blank(row["source_type"]),
            "source_label": blank(row["source_label"]),
            "source_supplied_by": blank(row["source_supplied_by"]),
            "original_filename": blank(row["original_filename"]),
            "sha256": blank(row["sha256"]),
            "mapping_template_name": blank(row["mapping_template_name"]),
            "is_ai_generated": int(row["is_ai_generated"] or 0),
            "received_at": blank(row["received_at"]),
            "research_method": blank(row["research_method"]),
        }
        for row in conn.execute(sql, params)
    ]
