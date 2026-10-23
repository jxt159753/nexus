"""Shared UTC budget predicate. Called inside the reservation transaction."""

from nexus.core.storage.database import one, timestamp
from nexus.core.security.types import AccessDenied


def accounted_cost(calls) -> int:
    """Count ended read-only web failures at their full reservation, never zero.

    A completed exchange is distinct from a successful result or known billing.
    Cancelled/recovered exchanges stay unknown and cannot pass this check.
    """
    total = 0
    for call in calls:
        if call["outcome"] != "completed":
            raise AccessDenied("BUDGET_EXCEEDED")
        if call["cost_status"] == "calculated":
            total += call["cost_microunits"]
        elif (call["cost_status"] == "unknown" and dict(call).get("provider") == "deepseek-web"
              and call["web_step_id"] and call["finished_at"]):
            total += max(call["reserved_microunits"], call["cost_microunits"] or 0)
        else:
            raise AccessDenied("BUDGET_EXCEEDED")
    return total


async def budget_error(conn, config, now, *, reservation: int | None = None) -> str | None:
    reservation = config.model_reservation() if reservation is None else reservation
    if not config.enabled:
        return "MODEL_DISABLED"
    if now.date() > config.price_valid_until:
        return "MODEL_PRICING_EXPIRED"
    if reservation <= 0 or (config.cost_limits_enabled and reservation > config.task_cost_microunits):
        return "BUDGET_EXCEEDED"
    midnight = timestamp(now.replace(hour=0, minute=0, second=0, microsecond=0))
    budget = await one(conn, "SELECT coalesce(sum(CASE WHEN cost_status IN ('reserved','unknown') THEN max(reserved_microunits,coalesce(cost_microunits,0)) ELSE coalesce(cost_microunits,0) END),0),count(*) FROM model_calls WHERE created_at>=? OR cost_status IN ('reserved','unknown')", (midnight,))
    if (config.cost_limits_enabled and budget[0] + reservation > config.daily_cost_microunits) or budget[1] >= config.daily_calls:
        return "BUDGET_EXCEEDED"
    return None
