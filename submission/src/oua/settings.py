"""Runtime settings. Defaults live here; `config/agent.json` and CLI flags override them."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

SUBMISSION_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PACK = SUBMISSION_ROOT.parent


@dataclass
class Settings:
    pack_path: Path = DEFAULT_PACK
    db_path: Path = SUBMISSION_ROOT / "viewer_state" / "agent.sqlite"

    # Latency (brief section 7). The home page is only seen if it lands before the viewer reaches home
    # (3-40 s after the open in the simulator), so the page path has a much tighter internal target.
    decision_deadline_s: float = 60.0
    failure_after_s: float = 300.0
    page_target_s: float = 3.0

    # Call budgets per decision (brief section 7).
    max_model_calls: int = 8
    max_tool_calls: int = 20

    # Dependency timeouts and simulated service latencies (virtual time in replay).
    model_timeout_s: float = 1.5
    model_latency_s: float = 0.4
    store_timeout_s: float = 2.0
    store_retries: int = 1
    store_latency_s: float = 0.02
    breaker_cooldown_s: float = 60.0

    # Harness: how many decisions can run concurrently.
    workers: int = 4
    shed_model_after_queue_s: float = 5.0  # when a decision waited this long in the queue, skip optional model calls

    # Model provider. Business policy (thresholds, goal rules, copy) lives in config/agent_policy.json, not here.
    llm_provider: str = "mock"          # mock | anthropic
    llm_model: str = "claude-opus-5-5"  # used when llm_provider = anthropic
    llm_effort: str = "low"             # output_config.effort; low keeps the page path fast

    @classmethod
    def load(cls, path: Path | None = None, **overrides) -> "Settings":
        data: dict = {}
        cfg = path or SUBMISSION_ROOT / "config" / "agent.json"
        if cfg.exists():
            data.update(json.loads(cfg.read_text(encoding="utf-8")))
        data.update({k: v for k, v in overrides.items() if v is not None})
        known = {f.name: f for f in fields(cls)}
        kwargs = {}
        for key, value in data.items():
            if key not in known:
                continue
            if key in ("pack_path", "db_path"):
                value = Path(value)
                if not value.is_absolute():
                    value = (SUBMISSION_ROOT / value).resolve()
            kwargs[key] = value
        return cls(**kwargs)

    def as_dict(self) -> dict:
        return {k: str(v) if isinstance(v, Path) else v for k, v in asdict(self).items()}
