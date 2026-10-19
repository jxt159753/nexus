"""Shared UTC budget predicate. Called inside the reservation transaction."""

from nexus.core.storage.database import one, timestamp


async def budget_error(conn, config, now, *, reservation: int | None = None) -> str | None:
    reservation = config.task_cost_microunits if reservation is None else reservation
    if not config.enabled:
        return "MODEL_DISABLED"
    if now.date() > config.price_valid_until:
        return "MODEL_PRICING_EXPIRED"
    if reservation <= 0 or reservation > config.task_cost_microunits:
        return "BUDGET_EXCEEDED"
    midnight = timestamp(now.replace(hour=0, minute=0, second=0, microsecond=0))
    budget = await one(conn, "SELECT coalesce(sum(CASE WHEN cost_status IN ('reserved','unknown') THEN max(reserved_microunits,coalesce(cost_microunits,0)) ELSE coalesce(cost_microunits,0) END),0),count(*) FROM model_calls WHERE created_at>=? OR cost_status IN ('reserved','unknown')", (midnight,))
    if budget[0] + reservation > config.daily_cost_microunits or budget[1] >= config.daily_calls:
        return "BUDGET_EXCEEDED"
    return None
