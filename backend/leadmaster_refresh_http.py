"""Admin HTTP for LeadMaster refresh. Live confirm is fail-closed."""

from __future__ import annotations

from typing import Any

from db import get_connection
from leadmaster_refresh_confirm import (
    confirm_refresh_http,
    refresh_confirm_readiness,
)
from leadmaster_refresh_mapping import REFRESH_MAPPING_FIELDS
from leadmaster_refresh_policy import policy_catalog
from leadmaster_refresh_staging import (
    REFRESH_APPLY_DISABLED,
    RefreshApplyDisabled,
    RefreshLiveWriteError,
    assert_not_production_db,
    plan_refresh_batch,
    refresh_batch_view,
    save_refresh_mapping,
    save_refresh_policy,
    save_refresh_resolutions,
    upload_leadmaster_refresh,
)
from models import NorthStarUser


def refresh_meta() -> dict[str, Any]:
    return {
        "source_system": "LEADMASTER",
        "import_mode": "LEADMASTER_REFRESH",
        "apply_enabled": False,
        "live_writes_enabled": False,
        "live_confirm_enabled": False,
        "mapping_fields": list(REFRESH_MAPPING_FIELDS),
        "policy": policy_catalog(),
        "not_mapped": "NOT MAPPED",
        "confirm_message": "Live confirmation not enabled",
    }


def upload_refresh(*, client_id: int, actor: NorthStarUser, filename: str, content: bytes, worksheet: str = "") -> dict[str, Any]:
    return upload_leadmaster_refresh(
        client_id=client_id,
        actor=actor,
        filename=filename,
        content=content,
        worksheet=worksheet,
    )


def preview_refresh(*, client_id: int, batch_id: int, persist: bool = False) -> dict[str, Any]:
    if persist:
        assert_not_production_db()
    with get_connection() as conn:
        plan = plan_refresh_batch(
            conn, client_id=client_id, batch_id=batch_id, persist_summary=persist
        )
        if persist:
            conn.commit()
        return plan


def confirm_refresh_disabled() -> None:
    raise RefreshApplyDisabled(REFRESH_APPLY_DISABLED)


def confirm_refresh(
    *,
    client_id: int,
    batch_id: int,
    actor: NorthStarUser,
    expected_fingerprint: str,
) -> dict[str, Any]:
    return confirm_refresh_http(
        client_id=client_id,
        batch_id=batch_id,
        actor=actor,
        expected_fingerprint=expected_fingerprint,
    )


def readiness_refresh(*, client_id: int, batch_id: int, actor: NorthStarUser) -> dict[str, Any]:
    with get_connection() as conn:
        gate = refresh_confirm_readiness(
            conn, client_id=client_id, batch_id=batch_id, actor=actor
        )
    gate.pop("plan", None)
    return gate
