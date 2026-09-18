"""Multi-client access control and dashboard query foundations.

Architecture rules:
- Company status/notes/activities are always client-scoped via client_id /
  client_company_relationships — never universal company fields.
- Users only retrieve work for clients they are assigned to (unless administrator).
- Dashboard can operate in "all my clients" or "selected client" mode.
"""

from __future__ import annotations

from db import DB_PATH, DEFAULT_USER_EMAIL, get_connection
from models import (
    ClientAssignment,
    ClientCompanyWorkItem,
    DashboardScopeSummary,
    NorthStarUser,
)


def _blank(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _db_exists() -> bool:
    return DB_PATH.exists()


def get_user_by_id(user_id: int) -> NorthStarUser | None:
    if not _db_exists():
        return None
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    return NorthStarUser(
        id=int(row["id"]),
        email=_blank(row["email"]),
        full_name=_blank(row["full_name"]),
        is_administrator=bool(row["is_administrator"]),
        is_internal_northstar=(
            bool(row["is_internal_northstar"])
            if "is_internal_northstar" in row.keys()
            else True
        ),
        active=bool(row["active"]),
        created_at=_blank(row["created_at"]),
    )


def get_user_by_email(email: str) -> NorthStarUser | None:
    if not _db_exists() or not email.strip():
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE lower(email) = lower(?)",
            (email.strip(),),
        ).fetchone()
    if row is None:
        return None
    return NorthStarUser(
        id=int(row["id"]),
        email=_blank(row["email"]),
        full_name=_blank(row["full_name"]),
        is_administrator=bool(row["is_administrator"]),
        is_internal_northstar=(
            bool(row["is_internal_northstar"])
            if "is_internal_northstar" in row.keys()
            else True
        ),
        active=bool(row["active"]),
        created_at=_blank(row["created_at"]),
    )


def get_default_user() -> NorthStarUser | None:
    return get_user_by_email(DEFAULT_USER_EMAIL)


def list_clients_for_user(
    user_id: int,
    *,
    active_only: bool = True,
) -> list[ClientAssignment]:
    """Clients the user may work — security boundary for non-administrators."""
    if not _db_exists():
        return []
    with get_connection() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None or not bool(user["active"]):
            return []

        if bool(user["is_administrator"]):
            rows = conn.execute(
                """
                SELECT
                    cl.id AS client_id,
                    cl.code AS client_code,
                    cl.name AS client_name,
                    COALESCE(uca.role, 'administrator') AS role,
                    COALESCE(uca.active, 1) AS active,
                    COALESCE(uca.assigned_at, cl.created_at) AS assigned_at
                FROM clients cl
                LEFT JOIN user_client_assignments uca
                    ON uca.client_id = cl.id AND uca.user_id = ?
                ORDER BY cl.name COLLATE NOCASE
                """,
                (user_id,),
            ).fetchall()
        else:
            sql = """
                SELECT
                    cl.id AS client_id,
                    cl.code AS client_code,
                    cl.name AS client_name,
                    uca.role,
                    uca.active,
                    uca.assigned_at
                FROM user_client_assignments uca
                JOIN clients cl ON cl.id = uca.client_id
                WHERE uca.user_id = ?
            """
            params: list[object] = [user_id]
            if active_only:
                sql += " AND uca.active = 1"
            sql += " ORDER BY cl.name COLLATE NOCASE"
            rows = conn.execute(sql, params).fetchall()

    return [
        ClientAssignment(
            client_id=int(r["client_id"]),
            client_code=_blank(r["client_code"]),
            client_name=_blank(r["client_name"]),
            role=_blank(r["role"]),
            active=bool(r["active"]),
            assigned_at=_blank(r["assigned_at"]),
        )
        for r in rows
    ]


WRITE_CLIENT_REQUIRED_MSG = (
    "client_id is required and must be a positive integer. "
    "All My Clients cannot be used for writes."
)


class WriteClientIdError(ValueError):
    """Missing, zero, or unknown client_id on a client-scoped write."""


def require_write_client_id(
    client_id: object,
    *,
    conn=None,
    user_id: int | None = None,
) -> int:
    """Require an explicit existing client for writes. Never infer Carmeco."""
    if client_id is None:
        raise WriteClientIdError(WRITE_CLIENT_REQUIRED_MSG)
    try:
        cid = int(client_id)
    except (TypeError, ValueError) as exc:
        raise WriteClientIdError(WRITE_CLIENT_REQUIRED_MSG) from exc
    if cid <= 0:
        raise WriteClientIdError(WRITE_CLIENT_REQUIRED_MSG)

    def _lookup(db) -> object:
        return db.execute("SELECT id FROM clients WHERE id = ?", (cid,)).fetchone()

    if conn is not None:
        row = _lookup(conn)
    else:
        if not _db_exists():
            raise WriteClientIdError(f"client_id {cid} does not exist.")
        with get_connection() as owned:
            row = _lookup(owned)
    if row is None:
        raise WriteClientIdError(f"client_id {cid} does not exist.")

    uid = user_id
    if uid is None:
        from staff_context import resolve_staff_actor

        actor = resolve_staff_actor()
        uid = int(actor.id) if actor is not None else None
    if uid is not None and not user_can_access_client(uid, cid):
        raise PermissionError("Not authorized for this client.")
    if uid is not None:
        from staff_rbac import user_has_permission

        if not user_has_permission(int(uid), "crm.edit", client_id=cid):
            raise PermissionError("Not authorized for this client.")
    return cid


def user_can_access_client(user_id: int, client_id: int) -> bool:
    """True if the user is assigned to the client, or is an administrator."""
    if not _db_exists():
        return False
    with get_connection() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None or not bool(user["active"]):
            return False
        if bool(user["is_administrator"]):
            return True
        row = conn.execute(
            """
            SELECT 1 FROM user_client_assignments
            WHERE user_id = ? AND client_id = ? AND active = 1
            """,
            (user_id, client_id),
        ).fetchone()
    return row is not None


def user_is_internal_northstar(user: NorthStarUser | None) -> bool:
    """Internal NorthStar employees may view cross-client history/search."""
    if user is None or not user.active:
        return False
    return bool(user.is_internal_northstar) or bool(user.is_administrator)


def resolve_visibility_client_ids(
    user_id: int,
    *,
    selected_client_id: int | None = None,
    purpose: str = "work",
) -> list[int]:
    """
    Visibility vs ownership:

    - purpose=\"work\" → Active Client / assigned clients (writes always use this).
    - purpose=\"search\" or \"shared_history\" for internal NorthStar users →
      all NorthStar clients (cross-client view).
    - external/client-facing users (is_internal_northstar=0) → assigned only,
      and selected_client_id is honored when provided.
    """
    user = get_user_by_id(user_id)
    if user is None or not user.active:
        raise PermissionError("User not found or inactive.")

    if purpose in {"search", "shared_history"} and user.is_administrator:
        if not _db_exists():
            return []
        with get_connection() as conn:
            rows = conn.execute("SELECT id FROM clients ORDER BY id").fetchall()
        return [int(r["id"]) for r in rows]

    return resolve_dashboard_client_ids(
        user_id, selected_client_id=selected_client_id, all_clients=False
    )


def resolve_dashboard_client_ids(
    user_id: int,
    *,
    selected_client_id: int | None = None,
    all_clients: bool = False,
) -> list[int]:
    """
    Dashboard scope:
    - all_clients=True → every NorthStar client (administrators only)
    - selected_client_id set → that client only (if permitted)
    - otherwise → all assigned clients ("All My Clients")
    """
    user = get_user_by_id(user_id)
    if user is None or not user.active:
        raise PermissionError("User not found or inactive.")

    if all_clients:
        if not user.is_administrator:
            raise PermissionError("All Clients mode requires administrator access.")
        if not _db_exists():
            return []
        with get_connection() as conn:
            rows = conn.execute("SELECT id FROM clients ORDER BY id").fetchall()
        return [int(r["id"]) for r in rows]

    assignments = list_clients_for_user(user_id, active_only=True)
    allowed = {a.client_id for a in assignments}

    if selected_client_id is None:
        return sorted(allowed)

    if selected_client_id in allowed:
        return [selected_client_id]

    if user.is_administrator:
        if not _db_exists():
            raise LookupError("Selected client not found.")
        with get_connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM clients WHERE id = ?",
                (selected_client_id,),
            ).fetchone()
        if exists is None:
            raise LookupError("Selected client not found.")
        return [selected_client_id]

    raise PermissionError("User is not assigned to the selected client.")


def list_client_company_work(
    user_id: int,
    *,
    selected_client_id: int | None = None,
) -> list[ClientCompanyWorkItem]:
    """
    Client-scoped company work for dashboard/KPI foundations.

    Status, priority, next_action, follow_up_date, and notes come from
    client_company_relationships — never from a universal companies.status.
    """
    client_ids = resolve_dashboard_client_ids(
        user_id, selected_client_id=selected_client_id
    )
    if not client_ids:
        return []

    placeholders = ",".join("?" * len(client_ids))
    sql = f"""
        SELECT
            ccr.id AS relationship_id,
            ccr.client_id,
            cl.code AS client_code,
            cl.name AS client_name,
            ccr.company_id,
            co.external_record_no,
            co.company_name,
            COALESCE(ccr.status, '') AS status,
            ccr.assigned_user_id,
            COALESCE(ccr.priority, '') AS priority,
            COALESCE(ccr.next_action, '') AS next_action,
            ccr.follow_up_date,
            COALESCE(ccr.notes, '') AS notes
        FROM client_company_relationships ccr
        JOIN clients cl ON cl.id = ccr.client_id
        JOIN companies co ON co.id = ccr.company_id
        WHERE ccr.client_id IN ({placeholders})
        ORDER BY cl.name COLLATE NOCASE, co.company_name COLLATE NOCASE
    """
    with get_connection() as conn:
        rows = conn.execute(sql, client_ids).fetchall()

    return [
        ClientCompanyWorkItem(
            relationship_id=int(r["relationship_id"]),
            client_id=int(r["client_id"]),
            client_code=_blank(r["client_code"]),
            client_name=_blank(r["client_name"]),
            company_id=int(r["company_id"]),
            external_record_no=_blank(r["external_record_no"]),
            company_name=_blank(r["company_name"]),
            status=_blank(r["status"]),
            assigned_user_id=(
                int(r["assigned_user_id"]) if r["assigned_user_id"] is not None else None
            ),
            priority=_blank(r["priority"]),
            next_action=_blank(r["next_action"]),
            follow_up_date=_blank(r["follow_up_date"]) or None,
            notes=_blank(r["notes"]),
        )
        for r in rows
    ]


def dashboard_scope_summary(
    user_id: int,
    *,
    selected_client_id: int | None = None,
) -> DashboardScopeSummary:
    """Aggregate counts for future Dashboard all-clients / selected-client modes."""
    from work_queue_data import list_due_work_items

    items = list_client_company_work(user_id, selected_client_id=selected_client_id)
    client_ids = resolve_dashboard_client_ids(
        user_id, selected_client_id=selected_client_id
    )

    def _status(item: ClientCompanyWorkItem) -> str:
        return item.status.lower()

    calls_due = 0
    follow_ups = 0
    for cid in client_ids:
        calls_due += len(list_due_work_items(kind="call", client_id=cid))
        follow_ups += len(list_due_work_items(kind="follow_up", client_id=cid))

    appointments = sum(1 for i in items if "appointment" in _status(i))
    hot = sum(1 for i in items if _status(i) == "hot prospect")
    new_assignments = sum(1 for i in items if _status(i) == "new")

    mode = "selected_client" if selected_client_id is not None else "all_my_clients"
    return DashboardScopeSummary(
        mode=mode,
        user_id=user_id,
        client_ids=client_ids,
        relationship_count=len(items),
        calls_due_today=calls_due,
        follow_ups_due=follow_ups,
        appointments_today=appointments,
        hot_prospects=hot,
        new_assignments=new_assignments,
    )
