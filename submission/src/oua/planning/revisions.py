"""Apply rule corrections only to open plans of the affected goal."""

from __future__ import annotations

from typing import Any

from oua.goals import GoalCatalog
from oua.state.store import Store


def apply_success_correction_to_open_plans(
    store: Store,
    catalog: GoalCatalog,
    goal_id: str,
    new_success: dict[str, Any],
    *,
    version_tag: str,
    effective_at: str,
) -> int:
    """Update catalog + open plans for one goal_id. Other viewers are untouched."""
    catalog.apply_success_correction(goal_id, new_success, version_tag=version_tag)
    store.register_goal_rule(goal_id, version_tag, new_success, effective_at)
    event = str(new_success["event"])
    condition = str(new_success.get("condition") or "")
    window_hours = int(new_success["window_hours"])
    updated = 0
    for plan in store.list_active_plans_for_goal(goal_id):
        previous_event = plan["success_event"]
        previous_version = plan["rule_version"]
        store.set_active_plan(
            viewer_id=plan["viewer_id"],
            goal_id=goal_id,
            deadline=plan["deadline"],
            success_event=event,
            success_condition=condition,
            window_hours=window_hours,
            rule_version=version_tag,
            started_at=plan["started_at"],
            decision_id=plan["decision_id"],
            replace_existing=True,
        )
        store.add_plan_revision(
            plan["viewer_id"],
            reason=f"goal_rule_corrected:{goal_id}",
            previous_goal_id=goal_id,
            new_goal_id=goal_id,
            previous_success_event=previous_event,
            new_success_event=event,
            previous_rule_version=previous_version,
            new_rule_version=version_tag,
            at=effective_at,
        )
        updated += 1
    return updated
