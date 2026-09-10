"""History-only company matching for Client Data Import.

Resolves history events to existing client-related companies only.
Never creates companies and never rewrites external_record_no.
"""

from __future__ import annotations

from dataclasses import dataclass

from client_data_history_aliases import history_company_alias_id
from import_brown_industries import norm_name
from shared_note_history_import import normalize_record_no


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


@dataclass(frozen=True, slots=True)
class HistoryCompanyMatch:
    status: str  # matched | unmatched | ambiguous
    method: str  # record_no | approved_alias | company_name | ""
    company_id: int | None = None
    company_name: str = ""
    external_record_no: str = ""
    candidate_ids: tuple[int, ...] = ()


def _company_by_record_no(conn, record_no: str):
    rn = normalize_record_no(record_no)
    if not rn:
        return None
    return conn.execute(
        """
        SELECT id, company_name, external_record_no
        FROM companies
        WHERE TRIM(COALESCE(external_record_no, '')) = ?
        LIMIT 2
        """,
        (rn,),
    ).fetchall()


def _company_by_id(conn, company_id: int):
    return conn.execute(
        """
        SELECT id, company_name, external_record_no
        FROM companies
        WHERE id = ?
        LIMIT 1
        """,
        (int(company_id),),
    ).fetchone()


def _company_on_client(conn, *, client_id: int, company_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM client_company_relationships
        WHERE client_id = ? AND company_id = ?
        LIMIT 1
        """,
        (int(client_id), int(company_id)),
    ).fetchone()
    return row is not None


def _client_companies_by_norm_name(conn, *, client_id: int, name: str) -> list:
    target = norm_name(name)
    if not target:
        return []
    rows = conn.execute(
        """
        SELECT c.id, c.company_name, c.external_record_no
        FROM companies c
        JOIN client_company_relationships ccr ON ccr.company_id = c.id
        WHERE ccr.client_id = ?
        """,
        (int(client_id),),
    ).fetchall()
    hits = []
    for row in rows:
        if norm_name(row["company_name"] or "") == target:
            hits.append(row)
    return hits


def resolve_history_company(
    conn,
    *,
    client_id: int,
    record_no: str,
    company_name: str,
) -> HistoryCompanyMatch:
    """Match Record No., then approved alias, then unique client-scoped name.

    Does not rewrite company.external_record_no. Never creates a company.
    """
    rn = normalize_record_no(record_no)
    if rn:
        hits = _company_by_record_no(conn, rn)
        if len(hits) == 1:
            row = hits[0]
            return HistoryCompanyMatch(
                status="matched",
                method="record_no",
                company_id=int(row["id"]),
                company_name=_blank(row["company_name"]),
                external_record_no=_blank(row["external_record_no"]),
                candidate_ids=(int(row["id"]),),
            )
        if len(hits) > 1:
            return HistoryCompanyMatch(
                status="ambiguous",
                method="record_no",
                candidate_ids=tuple(int(r["id"]) for r in hits),
            )

        alias_id = history_company_alias_id(client_id=int(client_id), record_no=rn)
        if alias_id is not None:
            row = _company_by_id(conn, alias_id)
            if row is not None and _company_on_client(
                conn, client_id=int(client_id), company_id=alias_id
            ):
                return HistoryCompanyMatch(
                    status="matched",
                    method="approved_alias",
                    company_id=int(row["id"]),
                    company_name=_blank(row["company_name"]),
                    external_record_no=_blank(row["external_record_no"]),
                    candidate_ids=(int(row["id"]),),
                )

    name = _blank(company_name)
    if name:
        name_hits = _client_companies_by_norm_name(
            conn, client_id=int(client_id), name=name
        )
        if len(name_hits) == 1:
            row = name_hits[0]
            return HistoryCompanyMatch(
                status="matched",
                method="company_name",
                company_id=int(row["id"]),
                company_name=_blank(row["company_name"]),
                external_record_no=_blank(row["external_record_no"]),
                candidate_ids=(int(row["id"]),),
            )
        if len(name_hits) > 1:
            return HistoryCompanyMatch(
                status="ambiguous",
                method="company_name",
                candidate_ids=tuple(int(r["id"]) for r in name_hits),
            )

    return HistoryCompanyMatch(status="unmatched", method="")
