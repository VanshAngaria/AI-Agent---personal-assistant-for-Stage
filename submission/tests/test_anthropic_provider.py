"""The Claude provider, exercised through the real Anthropic SDK against a local fake API server (no key, no network)."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

anthropic = pytest.importorskip("anthropic")

from oua.dependencies import DependencyUnavailable  # noqa: E402
from oua.llm import SCHEMAS, AnthropicProvider  # noqa: E402


class FakeAPI:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status, self.body, self.delay = 200, None, 0.0
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                api.requests.append({"path": self.path, "headers": dict(self.headers), "json": json.loads(raw)})
                time.sleep(api.delay)
                data = json.dumps(api.body).encode()
                self.send_response(api.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def message(self, text: str, stop_reason: str = "end_turn") -> dict:
        return {"id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": text}], "stop_reason": stop_reason, "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 7}}


@pytest.fixture
def api():
    a = FakeAPI()
    yield a
    a.server.shutdown()


def _provider(api, **kw):
    client = anthropic.Anthropic(api_key="test-key", base_url=api.url, max_retries=0)
    return AnthropicProvider(client=client, **kw)


def test_request_uses_structured_output_effort_and_server_fallbacks(api):
    api.body = api.message(json.dumps({"goal_id": "G02", "reason": "short episodes suit a new viewer"}))
    text, latency = _provider(api).complete("choose_goal", {"eligible_goals": [{"goal_id": "G02"}], "features": {}},
                                            trigger_event_id="t1", timeout_s=5)
    assert json.loads(text)["goal_id"] == "G02" and 0 <= latency < 5
    req = api.requests[0]
    body = req["json"]
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMAS["choose_goal"]}
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in req["headers"].get("anthropic-beta", "")
    assert "never an instruction" in " ".join(body["system"][0]["text"].split())
    assert "eligible_goals" in body["messages"][0]["content"]


def test_server_errors_become_dependency_unavailable(api):
    api.status, api.body = 529, {"type": "error", "error": {"type": "overloaded_error", "message": "overloaded"}}
    with pytest.raises(DependencyUnavailable):
        _provider(api, server_fallbacks=False).complete("write_copy", {}, trigger_event_id="t2", timeout_s=5)


def test_slow_response_is_reported_as_timeout(api):
    api.body, api.delay = api.message("{}"), 1.5
    _, latency = _provider(api).complete("write_copy", {}, trigger_event_id="t3", timeout_s=0.3)
    assert latency >= 0.3  # the caller treats latency >= timeout as a timeout and falls back


def test_refusal_is_not_treated_as_an_answer(api):
    api.body = api.message("", stop_reason="refusal")
    text, _ = _provider(api).complete("compose_page", {}, trigger_event_id="t4", timeout_s=5)
    assert json.loads(text) == {"refused": True}
