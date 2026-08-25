"""Cross-Client Opportunities — rules-based scoring and recommendation engine.

Privacy: other-client private notes, amounts, and references are never exposed
in opportunity payloads. Only client name, milestone type, and date are shown.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from access import (
    get_default_user,
    get_user_by_id,
    resolve_dashboard_client_ids,
    user_can_access_client,
)
from db import DB_PATH, get_connection
from models import (
    CrossClientOpportunity,
    CrossClientOpportunityList,
    OpportunityActionResult,
    OpportunityDismissRequest,
    OpportunityReviewRequest,
    OpportunitySignal,
    ScoreConfig,
)

# Configurable weights for future AI scoring replacement
DEFAULT_SCORE_CONFIG = ScoreConfig(
    purchase_order=40,
    quote=28,
    appointment_set=18,
    weblead=10,
    hot=10,
    multi_client_bonus=6,
    recency_90_days=10,
    recency_180_days=5,
    recency_365_days=2,
    never_worked_bonus=15,
    has_target_contacts_bonus=4,
)

SIGNAL_TYPES = ("Purchase Order", "Quote", "Appointment Set", "WebLead", "Hot")

EXCLUDE_TARGET_MILESTONES = frozenset({"Purchase Order", "Quote", "Appointment Set"})
EXCLUDE_TARGET_STATUSES = frozenset(
    {
        "current customer",
        "appointment set",
        "closed",
    }
)
ACTIVE_TARGET_STATUSES = frozenset(
    {
        "new",
        "left message",
        "contacted",
        "send information",
        "hot prospect",
        "obtained new contact name/number",
        "qualified",
        "good fit-but no projects at this time",
        "future/nurture",
    }
)

DISMISS_REASONS = frozenset(
    {
        # Current actionable reasons
        "Already pursuing",
        "Not a fit for this client",
        "Wrong location/facility",
        "Duplicate",
        "Do not pursue",
        "Other",
        # Legacy reasons (still accepted)
        "Not a Fit",
        "Already Known",
        "Wrong Industry",
        "Competitor",
        "Geography",
    }
)

REVIEW_STATUS_NEW = "New Opportunity"
REVIEW_STATUS_ADDED = "Added to Work Queue"
REVIEW_STATUS_REVIEWED = "Reviewed"
REVIEW_STATUS_DISMISSED = "Dismissed"


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _db_exists() -> bool:
    return DB_PATH.exists()


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _parse_date(value: str) -> datetime | None:
    text = _blank(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19].replace("T", " "), fmt if " " in text[:19] else "%Y-%m-%d")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", ""))
    except ValueError:
        return None


def _score_label(score: int) -> str:
    if score >= 90:
        return "Exceptional"
    if score >= 75:
        return "Strong"
    if score >= 60:
        return "Possible"
    return "Low"


def _display_target_status(status: str) -> str:
    """Blank / missing target status displays as New for prospecting."""
    text = _blank(status)
    if not text or text.lower() in {"(none)", "none"}:
        return "New"
    return text


def _recommended_action(
    *,
    score: int,
    target_client_name: str,
    proven_buyer: bool,
) -> str:
    target = target_client_name.strip() or "target client"
    if proven_buyer or score >= 75:
        return f"Prioritize for {target} outreach"
    if score >= 40:
        return f"Review for {target} outreach"
    return f"Evaluate fit for {target}"


def _why_summary(*, source_names: list[str], signal_types: list[str]) -> str:
    if "Purchase Order" in signal_types or "Quote" in signal_types:
        return (
            "Another NorthStar client has already generated meaningful commercial "
            "activity with this company."
        )
    if "Appointment Set" in signal_types:
        return (
            "Another NorthStar client has already engaged this company "
            "(appointment set)."
        )
    if source_names:
        return (
            f"{', '.join(source_names)} has NorthStar commercial signals "
            "worth reviewing for the target client."
        )
    return "Cross-client NorthStar signals suggest this company may be worth pursuing."


def _signal_weight(milestone_type: str, cfg: ScoreConfig) -> int:
    mapping = {
        "Purchase Order": cfg.purchase_order,
        "Quote": cfg.quote,
        "Appointment Set": cfg.appointment_set,
        "WebLead": cfg.weblead,
        "Hot": cfg.hot,
    }
    return int(mapping.get(milestone_type, 0))


def _classify_target_activity(
    *,
    has_relationship: bool,
    status: str,
    milestone_types: set[str],
    activity_count: int,
) -> str:
    status_l = status.lower()
    if not has_relationship and activity_count == 0 and not milestone_types:
        return "Never Worked"
    if milestone_types & EXCLUDE_TARGET_MILESTONES or status_l in {
        "current customer",
        "appointment set",
    }:
        return "Currently Active"
    if status_l in ACTIVE_TARGET_STATUSES or activity_count > 0 or milestone_types:
        if status_l in {"new", ""} and activity_count == 0 and not milestone_types:
            return "Never Worked"
        if status_l in ACTIVE_TARGET_STATUSES - {"new"} or activity_count > 0:
            return "Currently Active"
        return "Previously Worked"
    if has_relationship and status_l in {"", "new"} and activity_count == 0:
        return "Never Worked"
    return "Previously Worked"


def _compute_score(
    *,
    other_signals: list[OpportunitySignal],
    other_client_count: int,
    target_activity: str,
    target_contact_count: int,
    cfg: ScoreConfig,
) -> tuple[int, str, str]:
    if not other_signals:
        return 0, "Low", ""

    # Deduplicate type weights (take max weight per type, then sum with diminishing)
    by_type: dict[str, int] = {}
    latest: datetime | None = None
    for sig in other_signals:
        w = _signal_weight(sig.milestone_type, cfg)
        by_type[sig.milestone_type] = max(by_type.get(sig.milestone_type, 0), w)
        dt = _parse_date(sig.milestone_date)
        if dt and (latest is None or dt > latest):
            latest = dt

    # Strongest first; additional signal types add reduced value
    ordered = sorted(by_type.items(), key=lambda x: x[1], reverse=True)
    score = 0
    for i, (_t, w) in enumerate(ordered):
        score += w if i == 0 else max(4, w // 2)

    if other_client_count > 1:
        score += cfg.multi_client_bonus * min(other_client_count - 1, 3)

    if latest is not None:
        age = datetime.now() - latest
        if age <= timedelta(days=90):
            score += cfg.recency_90_days
        elif age <= timedelta(days=180):
            score += cfg.recency_180_days
        elif age <= timedelta(days=365):
            score += cfg.recency_365_days

    if target_activity == "Never Worked":
        score += cfg.never_worked_bonus
    elif target_activity == "Previously Worked":
        score += 4
    else:
        score -= 12

    if target_contact_count > 0:
        score += cfg.has_target_contacts_bonus

    score = max(0, min(100, score))
    strongest = ordered[0][0] if ordered else ""
    return score, _score_label(score), strongest


def list_cross_client_opportunities(
    user_id: int | None = None,
    *,
    target_client_id: int | None = None,
    signal_type: str | None = None,
    other_client_id: int | None = None,
    state: str | None = None,
    strength: str | None = None,
    target_status: str | None = None,
    target_activity: str | None = None,
    review_status: str | None = None,
    min_score: int | None = None,
    proven_buyer: bool = False,
    engaged_elsewhere: bool = False,
    multiple_signals: bool = False,
    include_active_target: bool = False,
    include_dismissed: bool = False,
    score_config: ScoreConfig | None = None,
) -> CrossClientOpportunityList:
    """
    Rank companies with other-client milestones that may be opportunities
    for the selected Target Client. One row per master company.
    """
    cfg = score_config or DEFAULT_SCORE_CONFIG
    user = get_user_by_id(user_id) if user_id is not None else get_default_user()
    if user is None:
        raise PermissionError("User not found.")

    allowed = resolve_dashboard_client_ids(user.id, selected_client_id=None)
    if not allowed:
        return CrossClientOpportunityList(
            target_client_id=0,
            target_client_name="",
            count=0,
            data_note="No assigned clients for this user.",
        )

    focus_id = target_client_id if target_client_id is not None else allowed[0]
    if focus_id not in allowed and not user.is_administrator:
        raise PermissionError("User is not assigned to the selected Target Client.")
    if user.is_administrator and focus_id not in allowed:
        # Admin may target any client
        if not user_can_access_client(user.id, focus_id):
            raise PermissionError("Target Client not found or not accessible.")

    if not _db_exists():
        return CrossClientOpportunityList(
            target_client_id=focus_id,
            target_client_name="",
            count=0,
        )

    with get_connection() as conn:
        target = conn.execute(
            "SELECT id, code, name FROM clients WHERE id = ?",
            (focus_id,),
        ).fetchone()
        if target is None:
            raise LookupError("Target Client not found.")

        client_count = conn.execute("SELECT COUNT(*) AS n FROM clients").fetchone()
        data_note = ""
        if int(client_count["n"] or 0) < 2:
            data_note = (
                "Only one NorthStar client is loaded in this database (Carmeco). "
                "True cross-client opportunities require additional client datasets. "
                "Showing 0 legitimate opportunities."
            )

        # Other-client milestone signals (privacy-safe fields only)
        other_rows = conn.execute(
            """
            SELECT
                rm.company_id,
                rm.client_id,
                cl.code AS client_code,
                cl.name AS client_name,
                rm.milestone_type,
                rm.milestone_date
            FROM revenue_milestones rm
            JOIN clients cl ON cl.id = rm.client_id
            WHERE rm.client_id != ?
              AND rm.milestone_type IN ('Purchase Order', 'Quote', 'Appointment Set', 'WebLead', 'Hot')
            ORDER BY rm.company_id, rm.milestone_date DESC
            """,
            (focus_id,),
        ).fetchall()

        by_company: dict[int, list] = {}
        for r in other_rows:
            by_company.setdefault(int(r["company_id"]), []).append(r)

        dismissed: set[int] = set()
        assigned: set[int] = set()
        reviewed: set[int] = set()
        if _table_exists(conn, "opportunity_dismissals"):
            for r in conn.execute(
                "SELECT company_id FROM opportunity_dismissals WHERE target_client_id = ?",
                (focus_id,),
            ):
                dismissed.add(int(r["company_id"]))
        if _table_exists(conn, "opportunity_assignments"):
            for r in conn.execute(
                "SELECT company_id FROM opportunity_assignments WHERE target_client_id = ?",
                (focus_id,),
            ):
                assigned.add(int(r["company_id"]))
        if _table_exists(conn, "opportunity_reviews"):
            for r in conn.execute(
                "SELECT company_id FROM opportunity_reviews WHERE target_client_id = ?",
                (focus_id,),
            ):
                reviewed.add(int(r["company_id"]))

        review_filter = _blank(review_status)
        want_dismissed = include_dismissed or review_filter.lower() in {
            "dismissed",
            REVIEW_STATUS_DISMISSED.lower(),
        }

        opportunities: list[CrossClientOpportunity] = []
        for company_id, rows in by_company.items():
            is_dismissed = company_id in dismissed
            if is_dismissed and not want_dismissed:
                continue

            company = conn.execute(
                """
                SELECT id, external_record_no, company_name, city, state
                FROM companies WHERE id = ?
                """,
                (company_id,),
            ).fetchone()
            if company is None:
                continue

            if state and _blank(company["state"]).upper() != state.strip().upper():
                continue

            signals = [
                OpportunitySignal(
                    client_id=int(r["client_id"]),
                    client_code=_blank(r["client_code"]),
                    client_name=_blank(r["client_name"]),
                    milestone_type=_blank(r["milestone_type"]),
                    milestone_date=_blank(r["milestone_date"]),
                )
                for r in rows
            ]

            if other_client_id is not None:
                signals = [s for s in signals if s.client_id == other_client_id]
                if not signals:
                    continue

            if signal_type:
                # Accept "Appointment" as alias for Appointment Set
                wanted = signal_type.strip()
                if wanted.lower() == "appointment":
                    wanted = "Appointment Set"
                if wanted not in {s.milestone_type for s in signals}:
                    continue

            type_set = {s.milestone_type for s in signals}
            if multiple_signals and len(type_set) < 2:
                continue

            other_clients = sorted(
                {
                    (s.client_id, s.client_code, s.client_name)
                    for s in signals
                },
                key=lambda x: x[2].lower(),
            )
            source_names = [c[2] for c in other_clients if c[2]]

            # Target-client state
            rel = conn.execute(
                """
                SELECT id, status, is_hot, external_record_no FROM client_company_relationships
                WHERE client_id = ? AND company_id = ?
                """,
                (focus_id, company_id),
            ).fetchone()
            target_status_val = _blank(rel["status"]) if rel else ""
            target_types = set()
            if rel is not None:
                for r in conn.execute(
                    """
                    SELECT DISTINCT milestone_type FROM revenue_milestones
                    WHERE client_id = ? AND company_id = ?
                    """,
                    (focus_id, company_id),
                ):
                    target_types.add(_blank(r["milestone_type"]))

            act_count = 0
            last_activity = ""
            act_row = conn.execute(
                """
                SELECT COUNT(*) AS n,
                       MAX(activity_at) AS last_at
                FROM activities
                WHERE client_id = ? AND company_id = ?
                """,
                (focus_id, company_id),
            ).fetchone()
            if act_row:
                act_count = int(act_row["n"] or 0)
                last_activity = _blank(act_row["last_at"])

            contact_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM contacts WHERE company_id = ?",
                    (company_id,),
                ).fetchone()["n"]
                or 0
            )

            activity_class = _classify_target_activity(
                has_relationship=rel is not None,
                status=target_status_val,
                milestone_types=target_types,
                activity_count=act_count,
            )

            # Default exclusion: already meaningful for target
            significant = bool(target_types & EXCLUDE_TARGET_MILESTONES) or (
                target_status_val.lower() in EXCLUDE_TARGET_STATUSES
            )
            if significant and not include_active_target:
                continue

            if target_activity and target_activity != "Any":
                if activity_class != target_activity:
                    continue

            display_status = _display_target_status(target_status_val)
            if target_status:
                wanted_status = target_status.strip().lower()
                if wanted_status == "(none)":
                    if target_status_val:
                        continue
                elif display_status.lower() != wanted_status and target_status_val.lower() != wanted_status:
                    continue

            has_other_po = "Purchase Order" in type_set
            has_target_po = "Purchase Order" in target_types
            is_proven = has_other_po and not has_target_po
            if proven_buyer and not is_proven:
                continue

            is_engaged = bool(type_set) and activity_class in {
                "Never Worked",
                "Previously Worked",
            }
            if engaged_elsewhere and not is_engaged:
                continue

            score, label, strongest = _compute_score(
                other_signals=signals,
                other_client_count=len(other_clients),
                target_activity=activity_class,
                target_contact_count=contact_count,
                cfg=cfg,
            )

            if min_score is not None and score < int(min_score):
                continue

            if strength:
                strength_l = strength.strip().lower()
                if strength_l == "exceptional" and label != "Exceptional":
                    continue
                if strength_l == "strong" and label != "Strong":
                    continue
                if strength_l == "possible" and label != "Possible":
                    continue
                if strength_l == "low" and label != "Low":
                    continue

            if is_dismissed:
                status_label = REVIEW_STATUS_DISMISSED
            elif company_id in assigned:
                status_label = REVIEW_STATUS_ADDED
            elif company_id in reviewed:
                status_label = REVIEW_STATUS_REVIEWED
            else:
                status_label = REVIEW_STATUS_NEW

            if review_filter:
                rf = review_filter.lower()
                aliases = {
                    "new": REVIEW_STATUS_NEW.lower(),
                    "new opportunity": REVIEW_STATUS_NEW.lower(),
                    "added": REVIEW_STATUS_ADDED.lower(),
                    "added to work queue": REVIEW_STATUS_ADDED.lower(),
                    "reviewed": REVIEW_STATUS_REVIEWED.lower(),
                    "dismissed": REVIEW_STATUS_DISMISSED.lower(),
                }
                wanted = aliases.get(rf, rf)
                if status_label.lower() != wanted:
                    continue

            # Prefer target-client Record No when present so workspace opens under TARGET
            workspace_rn = _blank(company["external_record_no"])
            if rel is not None:
                rel_rn = _blank(rel["external_record_no"])
                if rel_rn:
                    workspace_rn = rel_rn
                elif workspace_rn:
                    pass
            workspace_path = (
                f"/companies/{workspace_rn}?client_id={focus_id}"
                if workspace_rn
                else ""
            )

            opportunities.append(
                CrossClientOpportunity(
                    company_id=company_id,
                    external_record_no=workspace_rn or _blank(company["external_record_no"]),
                    company_name=_blank(company["company_name"]),
                    city=_blank(company["city"]),
                    state=_blank(company["state"]),
                    target_client_id=focus_id,
                    target_client_code=_blank(target["code"]),
                    target_client_name=_blank(target["name"]),
                    other_clients=[
                        {"client_id": c[0], "client_code": c[1], "client_name": c[2]}
                        for c in other_clients
                    ],
                    strongest_signal=strongest,
                    signal_history=signals,
                    signal_types=sorted(type_set),
                    target_client_status=display_status,
                    target_client_activity=activity_class,
                    last_target_activity=last_activity or None,
                    opportunity_score=score,
                    score_label=label,
                    proven_buyer=is_proven,
                    engaged_elsewhere=is_engaged,
                    has_target_relationship=rel is not None,
                    workspace_path=workspace_path,
                    review_status=status_label,
                    recommended_action=_recommended_action(
                        score=score,
                        target_client_name=_blank(target["name"]),
                        proven_buyer=is_proven,
                    ),
                    why_summary=_why_summary(
                        source_names=source_names,
                        signal_types=sorted(type_set),
                    ),
                    source_client_names=source_names,
                    # legacy fields for older callers
                    focus_client_id=focus_id,
                    other_client_id=other_clients[0][0] if other_clients else 0,
                    other_client_code=other_clients[0][1] if other_clients else "",
                    other_client_name=other_clients[0][2] if other_clients else "",
                    other_milestone_types=sorted(type_set),
                    reason=(
                        "Proven buyer elsewhere"
                        if is_proven
                        else "Engaged with another NorthStar client"
                    ),
                )
            )

        opportunities.sort(
            key=lambda o: (-o.opportunity_score, o.company_name.lower()),
        )

        return CrossClientOpportunityList(
            target_client_id=focus_id,
            target_client_code=_blank(target["code"]),
            target_client_name=_blank(target["name"]),
            count=len(opportunities),
            opportunities=opportunities,
            score_config=cfg,
            data_note=data_note,
        )


def opportunity_count_for_dashboard(
    user_id: int | None = None,
    *,
    target_client_id: int | None = None,
) -> int:
    result = list_cross_client_opportunities(
        user_id,
        target_client_id=target_client_id,
    )
    return result.count


def dismiss_opportunity(body: OpportunityDismissRequest) -> OpportunityActionResult:
    if body.reason not in DISMISS_REASONS:
        raise ValueError(
            f"Invalid dismissal reason. Allowed: {', '.join(sorted(DISMISS_REASONS))}."
        )
    user = get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    if not user_can_access_client(user.id, body.target_client_id) and not user.is_administrator:
        raise PermissionError("Not authorized for Target Client.")

    with get_connection() as conn:
        company = conn.execute(
            "SELECT id, external_record_no, company_name FROM companies WHERE id = ?",
            (body.company_id,),
        ).fetchone()
        if company is None:
            raise LookupError("Company not found.")
        conn.execute(
            """
            INSERT INTO opportunity_dismissals (
                target_client_id, company_id, dismissed_by, reason, notes, dismissed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(target_client_id, company_id) DO UPDATE SET
                dismissed_by = excluded.dismissed_by,
                reason = excluded.reason,
                notes = excluded.notes,
                dismissed_at = excluded.dismissed_at
            """,
            (
                body.target_client_id,
                body.company_id,
                body.dismissed_by.strip() or user.full_name,
                body.reason,
                body.notes,
                _now_iso(),
            ),
        )
        conn.commit()
    return OpportunityActionResult(
        action="dismiss",
        company_id=body.company_id,
        external_record_no=_blank(company["external_record_no"]),
        target_client_id=body.target_client_id,
        message=f"Dismissed for target client ({body.reason}).",
        workspace_path=(
            f"/companies/{_blank(company['external_record_no'])}"
            f"?client_id={body.target_client_id}"
        ),
    )


def mark_opportunity_reviewed(body: OpportunityReviewRequest) -> OpportunityActionResult:
    """Mark opportunity Reviewed for TARGET client only (does not create work)."""
    user = get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    if not user_can_access_client(user.id, body.target_client_id) and not user.is_administrator:
        raise PermissionError("Not authorized for Target Client.")

    with get_connection() as conn:
        company = conn.execute(
            "SELECT id, external_record_no, company_name FROM companies WHERE id = ?",
            (body.company_id,),
        ).fetchone()
        if company is None:
            raise LookupError("Company not found.")
        if not _table_exists(conn, "opportunity_reviews"):
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS opportunity_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_client_id INTEGER NOT NULL,
                    company_id INTEGER NOT NULL,
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (target_client_id, company_id)
                )
                """
            )
        # Clear dismissal so Reviewed is visible again if previously dismissed
        if _table_exists(conn, "opportunity_dismissals"):
            conn.execute(
                """
                DELETE FROM opportunity_dismissals
                WHERE target_client_id = ? AND company_id = ?
                """,
                (body.target_client_id, body.company_id),
            )
        conn.execute(
            """
            INSERT INTO opportunity_reviews (
                target_client_id, company_id, reviewed_by, notes, reviewed_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(target_client_id, company_id) DO UPDATE SET
                reviewed_by = excluded.reviewed_by,
                notes = excluded.notes,
                reviewed_at = excluded.reviewed_at
            """,
            (
                body.target_client_id,
                body.company_id,
                body.reviewed_by.strip() or user.full_name,
                body.notes,
                _now_iso(),
            ),
        )
        conn.commit()
    return OpportunityActionResult(
        action="reviewed",
        company_id=body.company_id,
        external_record_no=_blank(company["external_record_no"]),
        target_client_id=body.target_client_id,
        message="Marked Reviewed for target client.",
        workspace_path=(
            f"/companies/{_blank(company['external_record_no'])}"
            f"?client_id={body.target_client_id}"
        ),
    )


def add_opportunity_to_target(
    *,
    target_client_id: int,
    company_id: int,
    status: str = "New",
    created_by: str = "Julie Magnani",
    opportunity_score: int | None = None,
    source_summary: str = "",
    target_campaign_id: int | None = None,
) -> OpportunityActionResult:
    """
    Create/activate the client-company relationship for Target Client.
    Never duplicates the master companies row.
    Creates a Cross-Client Opportunity work item for the TARGET client only.
    """
    user = get_default_user()
    if user is None:
        raise PermissionError("User not found.")
    if not user_can_access_client(user.id, target_client_id) and not user.is_administrator:
        raise PermissionError("Not authorized for Target Client.")

    with get_connection() as conn:
        company = conn.execute(
            "SELECT id, external_record_no, company_name FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        if company is None:
            raise LookupError("Company not found.")
        client = conn.execute(
            "SELECT id, name FROM clients WHERE id = ?",
            (target_client_id,),
        ).fetchone()
        if client is None:
            raise LookupError("Target Client not found.")

        # Source intelligence (other clients only) — privacy-safe names
        source_names: list[str] = []
        for r in conn.execute(
            """
            SELECT DISTINCT cl.name AS client_name
            FROM revenue_milestones rm
            JOIN clients cl ON cl.id = rm.client_id
            WHERE rm.company_id = ?
              AND rm.client_id != ?
              AND rm.milestone_type IN ('Purchase Order', 'Quote', 'Appointment Set', 'WebLead', 'Hot')
            ORDER BY cl.name COLLATE NOCASE
            """,
            (company_id, target_client_id),
        ):
            name = _blank(r["client_name"])
            if name:
                source_names.append(name)
        resolved_source = _blank(source_summary) or ", ".join(source_names)

        score_val = int(opportunity_score) if opportunity_score is not None else 0
        if opportunity_score is None:
            # Best-effort score from current engine without changing algorithm
            try:
                listed = list_cross_client_opportunities(
                    user.id,
                    target_client_id=target_client_id,
                    include_dismissed=True,
                )
                for opp in listed.opportunities:
                    if opp.company_id == company_id:
                        score_val = int(opp.opportunity_score)
                        if not resolved_source:
                            resolved_source = ", ".join(opp.source_client_names)
                        break
            except Exception:
                score_val = 0

        existing = conn.execute(
            """
            SELECT id, status FROM client_company_relationships
            WHERE client_id = ? AND company_id = ?
            """,
            (target_client_id, company_id),
        ).fetchone()
        now = _now_iso()
        created_new = False
        if existing is None:
            cur = conn.execute(
                """
                INSERT INTO client_company_relationships (
                    client_id, company_id, status, assigned_user_id,
                    priority, next_action, notes, is_hot, created_at, updated_at,
                    external_record_no
                ) VALUES (?, ?, ?, ?, '', ?, ?, 0, ?, ?, ?)
                """,
                (
                    target_client_id,
                    company_id,
                    status.strip() or "New",
                    user.id,
                    "Review cross-client opportunity",
                    (
                        f"Originated from Cross-Client Opportunity. "
                        f"Source intelligence: {resolved_source or 'other NorthStar client'}."
                    ),
                    now,
                    now,
                    _blank(company["external_record_no"]),
                ),
            )
            relationship_id = int(cur.lastrowid)
            created_new = True
        else:
            relationship_id = int(existing["id"])
            # Do not overwrite an existing meaningful status; only stamp origin note if empty
            conn.execute(
                """
                UPDATE client_company_relationships
                SET updated_at = ?,
                    notes = CASE
                        WHEN trim(COALESCE(notes, '')) = '' THEN ?
                        ELSE notes
                    END
                WHERE id = ?
                """,
                (
                    now,
                    (
                        f"Originated from Cross-Client Opportunity. "
                        f"Source intelligence: {resolved_source or 'other NorthStar client'}."
                    ),
                    relationship_id,
                ),
            )

        if _table_exists(conn, "opportunity_assignments"):
            cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(opportunity_assignments)").fetchall()
            }
            if "opportunity_score" in cols and "source_summary" in cols:
                if "target_campaign_id" in cols:
                    conn.execute(
                        """
                        INSERT INTO opportunity_assignments (
                            target_client_id, company_id, relationship_id,
                            originated_from, opportunity_score, source_summary,
                            created_by, created_at, target_campaign_id
                        ) VALUES (?, ?, ?, 'Cross-Client Opportunity', ?, ?, ?, ?, ?)
                        ON CONFLICT(target_client_id, company_id) DO UPDATE SET
                            relationship_id = excluded.relationship_id,
                            opportunity_score = excluded.opportunity_score,
                            source_summary = excluded.source_summary,
                            created_by = excluded.created_by,
                            created_at = excluded.created_at,
                            target_campaign_id = COALESCE(
                                excluded.target_campaign_id,
                                opportunity_assignments.target_campaign_id
                            )
                        """,
                        (
                            target_client_id,
                            company_id,
                            relationship_id,
                            score_val,
                            resolved_source,
                            created_by.strip() or user.full_name,
                            now,
                            target_campaign_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO opportunity_assignments (
                            target_client_id, company_id, relationship_id,
                            originated_from, opportunity_score, source_summary,
                            created_by, created_at
                        ) VALUES (?, ?, ?, 'Cross-Client Opportunity', ?, ?, ?, ?)
                        ON CONFLICT(target_client_id, company_id) DO UPDATE SET
                            relationship_id = excluded.relationship_id,
                            opportunity_score = excluded.opportunity_score,
                            source_summary = excluded.source_summary,
                            created_by = excluded.created_by,
                            created_at = excluded.created_at
                        """,
                        (
                            target_client_id,
                            company_id,
                            relationship_id,
                            score_val,
                            resolved_source,
                            created_by.strip() or user.full_name,
                            now,
                        ),
                    )
            else:
                conn.execute(
                    """
                    INSERT INTO opportunity_assignments (
                        target_client_id, company_id, relationship_id,
                        originated_from, created_by, created_at
                    ) VALUES (?, ?, ?, 'Cross-Client Opportunity', ?, ?)
                    ON CONFLICT(target_client_id, company_id) DO UPDATE SET
                        relationship_id = excluded.relationship_id,
                        created_by = excluded.created_by,
                        created_at = excluded.created_at
                    """,
                    (
                        target_client_id,
                        company_id,
                        relationship_id,
                        created_by.strip() or user.full_name,
                        now,
                    ),
                )

        # Clear dismissal if re-adding
        if _table_exists(conn, "opportunity_dismissals"):
            conn.execute(
                """
                DELETE FROM opportunity_dismissals
                WHERE target_client_id = ? AND company_id = ?
                """,
                (target_client_id, company_id),
            )
        conn.commit()

    return OpportunityActionResult(
        action="add_to_work_queue",
        company_id=company_id,
        external_record_no=_blank(company["external_record_no"]),
        target_client_id=target_client_id,
        relationship_id=relationship_id,
        created_new_relationship=created_new,
        message="Added to Work Queue",
        workspace_path=(
            f"/companies/{_blank(company['external_record_no'])}"
            f"?client_id={target_client_id}"
        ),
    )


def workspace_cross_client_banner(
    record_no: str,
    *,
    target_client_id: int | None = None,
    user_id: int | None = None,
) -> dict | None:
    """Privacy-safe workspace banner when company is a cross-client opportunity."""
    result = list_cross_client_opportunities(
        user_id,
        target_client_id=target_client_id,
        engaged_elsewhere=True,
    )
    for opp in result.opportunities:
        if opp.external_record_no == record_no.strip():
            return {
                "applicable": True,
                "target_client_name": opp.target_client_name,
                "target_client_activity": opp.target_client_activity,
                "target_client_status": opp.target_client_status,
                "evidence": [
                    {
                        "client_name": s.client_name,
                        "milestone_type": s.milestone_type,
                        "milestone_date": s.milestone_date,
                    }
                    for s in opp.signal_history
                ],
                "opportunity_score": opp.opportunity_score,
                "score_label": opp.score_label,
            }
    return {
        "applicable": False,
        "target_client_name": result.target_client_name,
        "evidence": [],
    }
