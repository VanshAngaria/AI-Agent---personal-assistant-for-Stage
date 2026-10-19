"""Shared service surface. LLM generation is not implemented in this stage."""

from __future__ import annotations

from datetime import datetime

from oua.config import Settings
from oua.goals import GoalCatalog, GoalRule
from oua.planning.revisions import apply_success_correction_to_open_plans
from oua.planning.selector import GoalSelector, SelectionResult
from oua.retrieval.catalogue import CatalogueIndex, RetrieveQuery, RetrievedTitle
from oua.retrieval.homepage import HomepageAssembler, HomepagePage
from oua.retrieval.policy import PolicyIndex
from oua.schemas import Decision, GoalRef
from oua.state.store import Store
from oua.viewer import ViewerSnapshot


class DecisionService:
    def __init__(
        self,
        store: Store,
        catalog: GoalCatalog,
        settings: Settings,
        *,
        titles: CatalogueIndex | None = None,
        policy: PolicyIndex | None = None,
    ) -> None:
        self.store = store
        self.catalog = catalog
        self.settings = settings
        self.selector = GoalSelector(catalog)
        self.titles = titles
        self.policy = policy
        self.homepage = HomepageAssembler(titles, policy) if titles and policy else None

    def validate_goal_against_catalog(self, goal: GoalRef) -> GoalRule:
        return self.catalog.require_measurable(goal)

    def persist(self, decision: Decision) -> Decision:
        if decision.goal is not None:
            self.validate_goal_against_catalog(decision.goal)
        self.store.save_decision(decision)
        return decision

    def load(self, decision_id: str) -> Decision | None:
        return self.store.get_decision(decision_id)

    def select_goal(
        self,
        snapshot: ViewerSnapshot,
        *,
        viewer_id: str | None = None,
    ) -> SelectionResult:
        active = self.store.get_active_plan(viewer_id or snapshot.viewer_id)
        active_id = None if active is None else str(active["goal_id"])
        result = self.selector.select(snapshot, active_goal_id=active_id)
        if (
            active is not None
            and result.goal is not None
            and result.goal.id != active["goal_id"]
        ):
            self.store.add_plan_revision(
                snapshot.viewer_id,
                reason="facts_changed_previous_goal_ineligible",
                previous_goal_id=str(active["goal_id"]),
                new_goal_id=result.goal.id,
                previous_success_event=str(active["success_event"]),
                new_success_event=result.goal.success_signal.event,
                previous_rule_version=str(active["rule_version"]),
                new_rule_version=result.goal.rule_version,
            )
        return result

    def apply_goal_rule_correction(
        self,
        goal_id: str,
        new_success: dict,
        *,
        version_tag: str,
        effective_at: str,
    ) -> int:
        return apply_success_correction_to_open_plans(
            self.store,
            self.catalog,
            goal_id,
            new_success,
            version_tag=version_tag,
            effective_at=effective_at,
        )

    def retrieve_titles(self, query: RetrieveQuery) -> list[RetrievedTitle]:
        if self.titles is None:
            raise RuntimeError("catalogue index is not configured")
        return self.titles.retrieve(query)

    def assemble_homepage(
        self,
        *,
        dialect: str,
        at: datetime,
        allow_premium: bool,
        query_text: str | None = None,
        unfinished_slug: str | None = None,
        watched_genres: list[str] | None = None,
    ) -> HomepagePage | None:
        if self.homepage is None:
            raise RuntimeError("homepage assembler is not configured")
        return self.homepage.assemble(
            dialect=dialect,
            at=at,
            allow_premium=allow_premium,
            query_text=query_text,
            unfinished_slug=unfinished_slug,
            watched_genres=watched_genres,
        )
