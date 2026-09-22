"""One-shot live Research Import confirm for RI-5B Premier batch 1 only.

This is not a general production confirm switch.
NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM still cannot authorize northstar.db.

The contract is bound to:
  live database identity (is_production_db)
  client_id = 4 (Premier)
  batch_id = 1
  file SHA-256 = f4f8256c6e2c2f0af11806d809c0ba929998b75390d10740780b34b3c00385da
  frozen fingerprint = 9a738ebd998830708f2b48e317c6635a9da3608d2b9ba337d4599d3115d2ff32
  authenticated administrator actor

After batch 1 is confirmed, the same contract only permits exact idempotent
replay (apply_research_import_confirm short-circuits on status=confirmed with
the stored fingerprint). Any other batch, client, SHA, fingerprint, or
non-admin actor is refused. The UI Confirm button stays disabled.
"""
from __future__ import annotations

from typing import Any

from models import NorthStarUser
from research_import_mapping import blank
from research_import_plan import (
    ISOLATED_CONFIRM_DISABLED,
    PRODUCTION_CONFIRM_DISABLED,
    confirm_enabled,
    is_production_path,
)
from research_import_schema import is_production_db

RI5B_CLIENT_ID = 4
RI5B_BATCH_ID = 1
RI5B_SHA256 = "f4f8256c6e2c2f0af11806d809c0ba929998b75390d10740780b34b3c00385da"
RI5B_FINGERPRINT = "9a738ebd998830708f2b48e317c6635a9da3608d2b9ba337d4599d3115d2ff32"
RI5B_GRANT = "ri5b_one_shot"
ISOLATED_GRANT = "isolated"


def actor_is_administrator(actor: NorthStarUser | None) -> bool:
    return bool(actor is not None and getattr(actor, "is_administrator", False))


def one_shot_contract_matches(
    *,
    client_id: int,
    batch_id: int,
    sha256: str,
    fingerprint: str,
    actor: NorthStarUser | None,
) -> bool:
    return (
        actor_is_administrator(actor)
        and int(client_id) == RI5B_CLIENT_ID
        and int(batch_id) == RI5B_BATCH_ID
        and blank(sha256) == RI5B_SHA256
        and blank(fingerprint) == RI5B_FINGERPRINT
    )


def production_one_shot_authorized(
    conn,
    *,
    client_id: int,
    batch_id: int,
    expected_fingerprint: str,
    actor: NorthStarUser | None,
) -> bool:
    """True only for the exact RI-5B live batch on live northstar.db."""
    if not is_production_db(conn):
        return False
    from research_import_staging import load_batch

    try:
        batch = load_batch(conn, int(client_id), int(batch_id), allow_confirmed=True)
    except Exception:
        return False
    return one_shot_contract_matches(
        client_id=int(client_id),
        batch_id=int(batch_id),
        sha256=str(batch.get("sha256") or ""),
        fingerprint=str(expected_fingerprint or ""),
        actor=actor,
    )


def assert_confirm_permitted(
    conn,
    *,
    client_id: int,
    batch_id: int,
    expected_fingerprint: str,
    actor: NorthStarUser | None,
) -> str:
    """Fail-closed confirm gate.

    Live northstar.db: only the RI-5B one-shot contract.
    Isolated DBs: NORTHSTAR_ALLOW_RESEARCH_IMPORT_CONFIRM=1 only.
    """
    if is_production_db(conn) or is_production_path():
        if production_one_shot_authorized(
            conn,
            client_id=int(client_id),
            batch_id=int(batch_id),
            expected_fingerprint=str(expected_fingerprint or ""),
            actor=actor,
        ):
            return RI5B_GRANT
        raise PermissionError(PRODUCTION_CONFIRM_DISABLED)
    if confirm_enabled(conn):
        return ISOLATED_GRANT
    raise PermissionError(ISOLATED_CONFIRM_DISABLED)


def one_shot_public_contract() -> dict[str, Any]:
    return {
        "client_id": RI5B_CLIENT_ID,
        "batch_id": RI5B_BATCH_ID,
        "sha256": RI5B_SHA256,
        "fingerprint": RI5B_FINGERPRINT,
        "expires": (
            "After batch 1 status=confirmed, this contract only allows exact "
            "fingerprint idempotent replay. It never authorizes another batch."
        ),
        "not_an_env_toggle": True,
        "ui_confirm_remains_disabled": True,
    }
