"""
Unit tests for llm_client.py. The Anthropic SDK client and the `claude`
subprocess are both replaced with fakes, so nothing here touches the network
or spends tokens. A live smoke test is deliberately not included; use
`python3 llm_client.py --check` for that.
"""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

import httpx2
import pytest

import anthropic
import llm_client
import optimizer_schema

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "n": {"type": "integer"}},
    "required": ["answer", "n"],
    "additionalProperties": False,
}
GOOD = {"answer": "yes", "n": 1}


# --- helpers ------------------------------------------------------------------


def _api_response(text: str, *, stop_reason: str = "end_turn", model: str = "claude-opus-5"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=None,
        content=[SimpleNamespace(type="text", text=text)],
        model=model,
        usage=SimpleNamespace(to_dict=lambda: {"input_tokens": 10, "output_tokens": 5}),
    )


class FakeAnthropic:
    """Stand-in for anthropic.Anthropic that records the request and returns a canned response."""

    instances: list["FakeAnthropic"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.beta_calls: list[dict] = []
        self.plain_calls: list[dict] = []
        self.response = _api_response(json.dumps(GOOD))
        self.raise_exc: Exception | None = None
        FakeAnthropic.instances.append(self)

        outer = self

        class _Messages:
            def __init__(self, sink):
                self._sink = sink

            def create(self, **kw):
                self._sink.append(kw)
                if outer.raise_exc is not None:
                    raise outer.raise_exc
                return outer.response

        self.beta = SimpleNamespace(messages=_Messages(self.beta_calls))
        self.messages = _Messages(self.plain_calls)


def _sdk_error(cls, status: int, message: str = "boom"):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request)
    return cls(message, response=response, body=None)


def _cli_payload(**overrides):
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(GOOD),
        "structured_output": GOOD,
        "session_id": "sess-123",
        "total_cost_usd": 0.0421,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "modelUsage": {"claude-opus-5": {"inputTokens": 10}},
    }
    payload.update(overrides)
    return payload


class FakeRun:
    """Stand-in for subprocess.run that records the call and returns a canned CompletedProcess."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=self.stdout, stderr=self.stderr)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start every test from known settings regardless of what .env loaded."""
    monkeypatch.setenv("LLM_MODEL", "claude-opus-5")
    monkeypatch.setenv("LLM_EFFORT", "high")
    monkeypatch.setenv("LLM_TIMEOUT_S", "42")
    monkeypatch.delenv("LLM_CLI_PATH", raising=False)
    monkeypatch.setenv("LLM_CLI_MAX_BUDGET_USD", "1.5")
    monkeypatch.setenv("LLM_API_OUTPUT_MODE", "format")
    FakeAnthropic.instances.clear()


@pytest.fixture
def fake_sdk(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    monkeypatch.setattr(llm_client.anthropic, "Anthropic", FakeAnthropic)
    return FakeAnthropic


@pytest.fixture
def fake_cli(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "cli")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-be-scrubbed")
    monkeypatch.setattr(llm_client, "_resolve_cli", lambda: "/fake/bin/claude")
    runner = FakeRun(stdout=json.dumps(_cli_payload()))
    monkeypatch.setattr(llm_client.subprocess, "run", runner)
    return runner


# --- configuration ------------------------------------------------------------


def test_backend_defaults_to_api(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    assert llm_client.get_backend() == "api"


def test_unknown_backend_is_a_config_error(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "grpc")
    with pytest.raises(llm_client.LLMConfigError):
        llm_client.complete_structured("s", "u", SCHEMA)


def test_bad_effort_is_a_config_error(fake_sdk, monkeypatch):
    monkeypatch.setenv("LLM_EFFORT", "turbo")
    with pytest.raises(llm_client.LLMConfigError):
        llm_client.complete_structured("s", "u", SCHEMA)


def test_schema_must_be_an_object_schema(fake_sdk):
    with pytest.raises(llm_client.LLMConfigError):
        llm_client.complete_structured("s", "u", {"type": "array"})


def test_empty_env_value_counts_as_unset(monkeypatch):
    monkeypatch.setenv("LLM_CLI_PATH", "   ")
    assert llm_client._env("LLM_CLI_PATH") is None


# --- api backend --------------------------------------------------------------


def test_api_backend_returns_structured_data(fake_sdk):
    result = llm_client.complete_structured("SYS", "USER", SCHEMA)

    assert result.data == GOOD
    assert result.backend == "api"
    assert result.model == "claude-opus-5"
    assert result.usage == {"input_tokens": 10, "output_tokens": 5}

    client = fake_sdk.instances[-1]
    assert client.kwargs["api_key"] == "sk-ant-test-not-real"
    assert client.kwargs["timeout"] == 42.0

    assert len(client.beta_calls) == 1 and not client.plain_calls
    call = client.beta_calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["system"] == "SYS"
    assert call["messages"] == [{"role": "user", "content": "USER"}]
    assert call["output_config"] == {
        "effort": "high",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    assert call["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in call["betas"]
    assert "thinking" not in call  # adaptive default, never forced


def test_api_backend_uses_ga_endpoint_when_fallbacks_disabled(fake_sdk, monkeypatch):
    monkeypatch.setattr(llm_client, "ENABLE_REFUSAL_FALLBACKS", False)
    llm_client.complete_structured("SYS", "USER", SCHEMA)
    client = fake_sdk.instances[-1]
    assert len(client.plain_calls) == 1 and not client.beta_calls
    assert "fallbacks" not in client.plain_calls[0]
    assert "betas" not in client.plain_calls[0]


def test_api_backend_honours_explicit_overrides(fake_sdk):
    llm_client.complete_structured("s", "u", SCHEMA, model="claude-sonnet-5", effort="low", max_tokens=512)
    call = fake_sdk.instances[-1].beta_calls[0]
    assert call["model"] == "claude-sonnet-5"
    assert call["max_tokens"] == 512
    assert call["output_config"]["effort"] == "low"


def test_api_missing_key_is_a_config_error(fake_sdk, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(llm_client.LLMConfigError):
        llm_client.complete_structured("s", "u", SCHEMA)


def test_api_refusal_raises_llm_refusal(fake_sdk, monkeypatch):
    def _make(**kwargs):
        client = FakeAnthropic(**kwargs)
        client.response = _api_response("", stop_reason="refusal")
        client.response.stop_details = SimpleNamespace(category="cyber", explanation="nope")
        return client

    monkeypatch.setattr(llm_client.anthropic, "Anthropic", _make)
    with pytest.raises(llm_client.LLMRefusal, match="cyber"):
        llm_client.complete_structured("s", "u", SCHEMA)


def test_api_truncation_raises_schema_error(fake_sdk, monkeypatch):
    def _make(**kwargs):
        client = FakeAnthropic(**kwargs)
        client.response = _api_response('{"answer": "ye', stop_reason="max_tokens")
        return client

    monkeypatch.setattr(llm_client.anthropic, "Anthropic", _make)
    with pytest.raises(llm_client.LLMSchemaError, match="max_tokens"):
        llm_client.complete_structured("s", "u", SCHEMA)


def test_api_missing_required_key_raises_schema_error(fake_sdk, monkeypatch):
    def _make(**kwargs):
        client = FakeAnthropic(**kwargs)
        client.response = _api_response(json.dumps({"answer": "yes"}))
        return client

    monkeypatch.setattr(llm_client.anthropic, "Anthropic", _make)
    with pytest.raises(llm_client.LLMSchemaError, match="missing required keys"):
        llm_client.complete_structured("s", "u", SCHEMA)


@pytest.mark.parametrize(
    "exc_cls, status, expected",
    [
        (anthropic.AuthenticationError, 401, llm_client.LLMAuthError),
        (anthropic.PermissionDeniedError, 403, llm_client.LLMAuthError),
        (anthropic.RateLimitError, 429, llm_client.LLMRateLimitError),
        (anthropic.InternalServerError, 503, llm_client.LLMBackendUnavailable),
        (anthropic.BadRequestError, 400, llm_client.LLMError),
    ],
)
def test_api_sdk_errors_map_to_llm_errors(fake_sdk, monkeypatch, exc_cls, status, expected):
    def _make(**kwargs):
        client = FakeAnthropic(**kwargs)
        client.raise_exc = _sdk_error(exc_cls, status)
        return client

    monkeypatch.setattr(llm_client.anthropic, "Anthropic", _make)
    with pytest.raises(expected):
        llm_client.complete_structured("s", "u", SCHEMA)


def test_api_connection_error_is_backend_unavailable(fake_sdk, monkeypatch):
    def _make(**kwargs):
        client = FakeAnthropic(**kwargs)
        client.raise_exc = anthropic.APIConnectionError(
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        return client

    monkeypatch.setattr(llm_client.anthropic, "Anthropic", _make)
    with pytest.raises(llm_client.LLMBackendUnavailable):
        llm_client.complete_structured("s", "u", SCHEMA)


# --- api backend: tool mode -------------------------------------------------------


def _tool_response(payload: dict, *, name: str = "submit_result", extra_text: str = ""):
    blocks = []
    if extra_text:
        blocks.append(SimpleNamespace(type="text", text=extra_text))
    blocks.append(SimpleNamespace(type="tool_use", name=name, id="toolu_1", input=payload))
    return SimpleNamespace(
        stop_reason="tool_use",
        stop_details=None,
        content=blocks,
        model="claude-opus-5",
        usage=SimpleNamespace(to_dict=lambda: {"input_tokens": 10, "output_tokens": 5}),
    )


def test_api_tool_mode_sends_strict_tool_and_reads_tool_use(fake_sdk, monkeypatch):
    monkeypatch.setenv("LLM_API_OUTPUT_MODE", "tool")

    def _make(**kwargs):
        client = FakeAnthropic(**kwargs)
        client.response = _tool_response(GOOD, name="submit_plan", extra_text="Here is the plan.")
        return client

    monkeypatch.setattr(llm_client.anthropic, "Anthropic", _make)
    result = llm_client.complete_structured("SYS", "USER", SCHEMA, tool_name="submit_plan", tool_description="Hand back the plan.")

    assert result.data == GOOD
    call = fake_sdk.instances[-1].beta_calls[0]
    assert call["tools"] == [{
        "name": "submit_plan",
        "description": "Hand back the plan.",
        "strict": True,
        "input_schema": SCHEMA,
    }]
    assert call["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert call["output_config"] == {"effort": "high"}  # no format block in tool mode


def test_api_tool_mode_is_the_default(fake_sdk, monkeypatch):
    monkeypatch.delenv("LLM_API_OUTPUT_MODE")

    def _make(**kwargs):
        client = FakeAnthropic(**kwargs)
        client.response = _tool_response(GOOD)
        return client

    monkeypatch.setattr(llm_client.anthropic, "Anthropic", _make)
    llm_client.complete_structured("s", "u", SCHEMA)
    assert "tools" in fake_sdk.instances[-1].beta_calls[0]


def test_api_tool_mode_text_only_answer_is_schema_error(fake_sdk, monkeypatch):
    monkeypatch.setenv("LLM_API_OUTPUT_MODE", "tool")
    with pytest.raises(llm_client.LLMSchemaError, match="did not call"):
        llm_client.complete_structured("s", "u", SCHEMA)  # default fake answers with a text block


def test_api_tool_mode_falls_back_to_format_for_fable(fake_sdk, monkeypatch):
    monkeypatch.setenv("LLM_API_OUTPUT_MODE", "tool")
    llm_client.complete_structured("s", "u", SCHEMA, model="claude-fable-5-1")
    call = fake_sdk.instances[-1].beta_calls[0]
    assert "tools" not in call
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}


def test_api_bad_output_mode_is_config_error(fake_sdk, monkeypatch):
    monkeypatch.setenv("LLM_API_OUTPUT_MODE", "xml")
    with pytest.raises(llm_client.LLMConfigError):
        llm_client.complete_structured("s", "u", SCHEMA)


def test_estimate_tokens_uses_count_tokens_when_key_present(fake_sdk, monkeypatch):
    monkeypatch.setenv("LLM_API_OUTPUT_MODE", "tool")
    seen = {}

    def _make(**kwargs):
        client = FakeAnthropic(**kwargs)

        def _count(**kw):
            seen.update(kw)
            return SimpleNamespace(input_tokens=1234)

        client.messages.count_tokens = _count
        return client

    monkeypatch.setattr(llm_client.anthropic, "Anthropic", _make)
    tokens, method = llm_client.estimate_tokens("SYS", "USER", SCHEMA, tool_name="submit_plan")
    assert (tokens, method) == (1234, "count_tokens")
    assert seen["system"] == "SYS"
    assert seen["tools"][0]["name"] == "submit_plan"


def test_estimate_tokens_heuristic_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    tokens, method = llm_client.estimate_tokens("a" * 400, "b" * 400, SCHEMA)
    assert method == "heuristic"
    assert tokens >= 200


# --- cli backend --------------------------------------------------------------


def test_cli_backend_returns_structured_data(fake_cli):
    result = llm_client.complete_structured("SYS", "USER", SCHEMA)

    assert result.data == GOOD
    assert result.backend == "cli"
    assert result.model == "claude-opus-5"
    assert result.cost_usd == pytest.approx(0.0421)
    assert result.session_id == "sess-123"
    assert result.usage == {"input_tokens": 10, "output_tokens": 5}

    cmd, kwargs = fake_cli.calls[0]
    assert cmd[0] == "/fake/bin/claude"
    assert cmd[1:3] == ["-p", "USER"]
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == SCHEMA
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS"
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5"
    assert cmd[cmd.index("--effort") + 1] == "high"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--no-session-persistence" in cmd
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--max-budget-usd") + 1] == "1.5"
    assert "--bare" not in cmd  # would disable subscription auth

    assert kwargs["timeout"] == 42.0
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert os.path.isdir(kwargs["cwd"])
    assert "CLAUDE.md" not in os.listdir(kwargs["cwd"])


def test_cli_backend_scrubs_api_key_and_session_vars_from_subprocess_env(fake_cli, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example")
    # What a shell opened by Claude Code exports:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_EFFORT", "xhigh")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-session")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    # What a headless host legitimately sets:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-keep-me")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/srv/claude-config")
    monkeypatch.setenv("PATH", "/usr/bin")
    llm_client.complete_structured("s", "u", SCHEMA)
    env = fake_cli.calls[0][1]["env"]
    for scrubbed in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        "CLAUDECODE", "CLAUDE_EFFORT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
    ):
        assert scrubbed not in env, scrubbed
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-keep-me"
    assert env["CLAUDE_CONFIG_DIR"] == "/srv/claude-config"
    assert env["PATH"] == "/usr/bin"
    assert env["LLM_BACKEND"] == "cli"  # our own settings pass through unchanged


def test_cli_backend_falls_back_to_result_text(fake_cli, monkeypatch):
    fake_cli.stdout = json.dumps(_cli_payload(structured_output=None))
    result = llm_client.complete_structured("s", "u", SCHEMA)
    assert result.data == GOOD


def test_cli_schema_failure_subtype_maps_to_schema_error(fake_cli):
    fake_cli.stdout = json.dumps(
        _cli_payload(subtype="error_max_structured_output_retries", is_error=True, structured_output=None)
    )
    fake_cli.returncode = 1
    with pytest.raises(llm_client.LLMSchemaError):
        llm_client.complete_structured("s", "u", SCHEMA)


def test_cli_budget_subtype_maps_to_budget_error(fake_cli):
    fake_cli.stdout = json.dumps(_cli_payload(subtype="error_max_budget_usd", is_error=True, result="budget hit"))
    fake_cli.returncode = 2
    with pytest.raises(llm_client.LLMBudgetExceeded):
        llm_client.complete_structured("s", "u", SCHEMA)


def test_cli_not_logged_in_maps_to_auth_error(fake_cli):
    fake_cli.stdout = ""
    fake_cli.stderr = "Not logged in · Please run /login"
    fake_cli.returncode = 1
    with pytest.raises(llm_client.LLMAuthError):
        llm_client.complete_structured("s", "u", SCHEMA)


def test_cli_missing_required_key_raises_schema_error(fake_cli):
    fake_cli.stdout = json.dumps(_cli_payload(structured_output={"answer": "yes"}))
    with pytest.raises(llm_client.LLMSchemaError):
        llm_client.complete_structured("s", "u", SCHEMA)


def test_cli_timeout_is_backend_unavailable(fake_cli, monkeypatch):
    def _timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(llm_client.subprocess, "run", _timeout)
    with pytest.raises(llm_client.LLMBackendUnavailable, match="timed out"):
        llm_client.complete_structured("s", "u", SCHEMA)


def test_cli_oversized_prompt_is_rejected_before_spawning(fake_cli):
    with pytest.raises(llm_client.LLMError, match="argv"):
        llm_client.complete_structured("s", "x" * (llm_client._MAX_CLI_PROMPT_CHARS + 1), SCHEMA)
    assert fake_cli.calls == []


def test_cli_binary_missing_is_backend_unavailable(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "cli")
    monkeypatch.setattr(llm_client.shutil, "which", lambda name: None)
    with pytest.raises(llm_client.LLMBackendUnavailable, match="LLM_CLI_PATH"):
        llm_client.complete_structured("s", "u", SCHEMA)


def test_cli_path_override_must_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_BACKEND", "cli")
    monkeypatch.setenv("LLM_CLI_PATH", str(tmp_path / "nope"))
    with pytest.raises(llm_client.LLMBackendUnavailable, match="does not exist"):
        llm_client.complete_structured("s", "u", SCHEMA)


# --- readiness probe ----------------------------------------------------------


def test_check_backend_cli_reports_login_state(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "cli")
    monkeypatch.setattr(llm_client, "_resolve_cli", lambda: "/fake/bin/claude")

    def _run(cmd, **kwargs):
        if cmd[1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, stdout="2.1.275 (Claude Code)\n", stderr="")
        assert cmd[1:] == ["auth", "status", "--json"]
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"loggedIn": False, "authMethod": "none"}), stderr=""
        )

    monkeypatch.setattr(llm_client.subprocess, "run", _run)
    info = llm_client.check_backend()
    assert info["backend"] == "cli"
    assert info["ok"] is False
    assert info["cli_version"] == "2.1.275 (Claude Code)"
    assert "setup-token" in info["hint"]


def test_check_backend_api_retrieves_model(fake_sdk, monkeypatch):
    def _make(**kwargs):
        client = FakeAnthropic(**kwargs)
        client.models = SimpleNamespace(retrieve=lambda model_id: SimpleNamespace(id=model_id))
        return client

    monkeypatch.setattr(llm_client.anthropic, "Anthropic", _make)
    info = llm_client.check_backend()
    assert info == {
        "backend": "api",
        "model": "claude-opus-5",
        "ok": True,
        "sdk_version": anthropic.__version__,
    }


# --- events contract ----------------------------------------------------------


def _assert_strict(schema: dict, path: str = "$") -> None:
    if schema.get("type") == "object":
        props = schema.get("properties", {})
        assert schema.get("additionalProperties") is False, f"{path}: additionalProperties must be false"
        assert set(schema.get("required", [])) == set(props), f"{path}: every property must be required"
        for name, sub in props.items():
            _assert_strict(sub, f"{path}.{name}")
    elif schema.get("type") == "array":
        _assert_strict(schema["items"], f"{path}[]")


def test_proposed_events_schema_is_strict_and_accepted_by_adapter(fake_sdk, monkeypatch):
    _assert_strict(optimizer_schema.PROPOSED_EVENTS_SCHEMA)

    sample = {
        "events": [
            {
                "item_type": "task",
                "item_id": "notion_abc",
                "title": "Write report",
                "description": "",
                "location": "",
                "start": "2026-09-21T09:00:00-04:00",
                "end": "2026-09-21T11:00:00-04:00",
                "all_day": False,
                "timezone": "America/New_York",
                "rationale": "First free morning before the deadline.",
            }
        ],
        "unscheduled": [],
        "notes": "",
    }

    def _make(**kwargs):
        client = FakeAnthropic(**kwargs)
        client.response = _api_response(json.dumps(sample))
        return client

    monkeypatch.setattr(llm_client.anthropic, "Anthropic", _make)
    result = llm_client.complete_structured("s", "u", optimizer_schema.PROPOSED_EVENTS_SCHEMA)
    assert result.data == sample
