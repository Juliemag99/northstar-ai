"""Client-scoped history Record No. → existing company aliases.

Used by history-only Client Data Import matching. Aliases never create
companies and never rewrite company.external_record_no.

Brown aliases are grounded in operator approvals and confirmed batch-16
prospect review decisions (not fuzzy name guessing).
"""

from __future__ import annotations

from shared_note_history_import import normalize_record_no

# client_id -> { source LeadMaster Record No. -> existing companies.id }
# Evidence notes are comments only; runtime uses the numeric map.
BROWN_HISTORY_RECORD_NO_ALIASES: dict[str, int] = {
    # Operator-approved (2026-09-09 validation review)
    "1325879": 751,  # Altec Worldwide → Altec Industries Inc
    "1325884": 751,  # Altec → Altec Industries Inc
    "1325878": 751,  # ALTEC → Altec Industries Inc
    "1374193": 465,  # JE Dunn Construction → JE Dunn…Off-Site
    "1325585": 465,  # JE Dunn Construction Co → JE Dunn…Off-Site
    "1393776": 215,  # Greenheck → Greenheck Fan
    "1326502": 467,  # City Electric Supply (TAMCO) → City Electric Supply
    "1390613": 878,  # nVent - Hoffman Enclosures Inc → nVent
    "1395393": 147,  # Sioux Chief → Sioux Chief Mfg. Co, Inc
    "1402279": 652,  # Cummins → Cummins Power Generation
    "1326501": 425,  # Ronson Manufacturing Corp → Ronson Manufacturing (Current Customer)
    # Confirmed batch-16 audit / Julie prospect decisions
    "1401940": 921,  # Williams Patent Crusher → PULVERIZER (same address; Corey Bellovich / williamscrusher.com on 921)
    "1326962": 79,  # The Massman Companies → Massman Automation Designs (rows 1807/1808)
    "1326946": 1034,  # Packaging Specialties Inc. → Pacmac (Julie row 1814; pacmac.com + shared phone)
    "1350965": 483,  # PM Peterson Vehicle Safety Lighting → Peterson Manufacturing Co (row 350)
    "1402335": 333,  # HIMOINSA Power Systems → HIPOWER Systems (row 1012)
}

HISTORY_RECORD_NO_ALIASES_BY_CLIENT: dict[int, dict[str, int]] = {
    2: BROWN_HISTORY_RECORD_NO_ALIASES,
}


def history_company_alias_id(*, client_id: int, record_no: str) -> int | None:
    """Return approved existing company_id for this client's source Record No."""
    rn = normalize_record_no(record_no)
    if not rn:
        return None
    mapping = HISTORY_RECORD_NO_ALIASES_BY_CLIENT.get(int(client_id)) or {}
    company_id = mapping.get(rn)
    if company_id is None:
        return None
    return int(company_id)
