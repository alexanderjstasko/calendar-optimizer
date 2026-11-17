"""
llm_client.py — the one place calendar_optimizer talks to Claude.

Two interchangeable backends sit behind a single function,
complete_structured(), chosen by LLM_BACKEND in the project-root .env:

    api  -> the Anthropic Python SDK, authenticated with ANTHROPIC_API_KEY.
            Always available, billed per token.
    cli  -> the Claude Code CLI in headless mode
            (`claude -p "<prompt>" --output-format json --json-schema ...`).
            Authenticates with whatever the CLI is logged into -- a
            subscription via `claude auth login`, or CLAUDE_CODE_OAUTH_TOKEN
            from `claude setup-token` on an unattended host -- so there is no
            separate bill while a subscription is active.

Both return the same LLMResult and raise the same LLMError family, so the
optimizer runner never needs to know which one ran. No other module should
import `anthropic` or spawn `claude` directly.

Configuration (read from the environment at call time, so tests and one-off
scripts can override without re-importing):

    LLM_BACKEND              api | cli                  (default: api)
    LLM_MODEL                full model id              (default: claude-opus-5)
    LLM_EFFORT               low|medium|high|xhigh|max  (default: high)
    LLM_TIMEOUT_S            per-call wall clock, secs  (default: 600)
    LLM_CLI_PATH             claude binary if not on PATH        (cli only)
    LLM_CLI_MAX_BUDGET_USD   passed as --max-budget-usd          (cli only, default 2)
    LLM_API_OUTPUT_MODE      tool | format                       (api only, default tool)

How the structured output is obtained:

    api / tool    the schema becomes a strict tool the model must call once;
                  the tool_use block's input is the result. tool_choice stays
                  "auto" (forced tool choice is rejected on Fable/Mythos and
                  clashes with adaptive thinking), so the system prompt tells
                  the model to call it. Fable/Mythos models fall back to format.
    api / format  output_config.format = json_schema; the text block is JSON.
    cli           --json-schema; the result's structured_output field.

Three CLI gotchas, verified against `claude --help` (v2.1.275) and baked in:

  * `--bare` never reads OAuth/keychain credentials, so it is deliberately NOT
    used -- it would silently fall back to the API key and bill per token.
  * In print mode the CLI prefers ANTHROPIC_API_KEY whenever it is present in
    the environment, and the project .env sets it. The subprocess therefore
    gets a scrubbed environment (see _cli_env) so it uses the subscription
    login instead.
  * A shell opened by Claude Code exports its own CLAUDE_* / CLAUDE_CODE_*
    session variables (CLAUDE_EFFORT, CLAUDE_CODE_SESSION_ID, CLAUDECODE, ...).
    Inherited by a child `claude`, they make it behave as a nested session
    and pick up the parent's effort level. They are scrubbed too, and this
    module's own settings use an LLM_ prefix so they can never collide.

Smoke-test the configured backend without spending any tokens:

    python3 llm_client.py --check
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

try:
    import anthropic
except ImportError:  # pragma: no cover - only when the SDK is not installed
    anthropic = None  # type: ignore[assignment]

# .env lives one directory above backend/, per project convention.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logger = logging.getLogger(__name__)

DEFAULT_BACKEND = "api"
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "high"
DEFAULT_TIMEOUT_S = 600.0
DEFAULT_MAX_TOKENS = 16000
DEFAULT_CLI_MAX_BUDGET_USD = "2"
DEFAULT_API_OUTPUT_MODE = "tool"
VALID_API_OUTPUT_MODES = ("tool", "format")
DEFAULT_TOOL_NAME = "submit_result"
# Models that reject forced tool use; they always get output_config.format.
_FORMAT_ONLY_MODEL_PREFIXES = ("claude-fable", "claude-mythos")
VALID_BACKENDS = ("api", "cli")
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Server-side refusal fallback, per the SDK guidance for Opus 5 / Fable 5.1
# code: if a safety classifier declines the request, the API re-runs it on a
# substitute model inside the same call. Flip to False to use the plain GA
# endpoint with no beta header.
ENABLE_REFUSAL_FALLBACKS = True
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Variables the CLI would pick up in preference to its subscription login,
# plus the flag Claude Code sets to detect that it is running inside itself.
_CLI_SCRUB_EXACT = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDECODE")
# Every other CLAUDE* variable is a Claude Code session leftover and is dropped,
# except the two a headless host legitimately sets for the CLI.
_CLI_SCRUB_PREFIX = "CLAUDE"
_CLI_KEEP = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")

# Prompts travel on argv for the cli backend. Linux caps a single argv string
# at 128 KiB (MAX_ARG_STRLEN), so refuse anything near that instead of letting
# the Pi fail with a cryptic "argument list too long".
_MAX_CLI_PROMPT_CHARS = 100_000

# One headless CLI process at a time: each one is a full Claude Code runtime,
# which matters on a Raspberry Pi with limited RAM.
_CLI_LOCK = threading.Lock()


# --- errors -----------------------------------------------------------------


class LLMError(Exception):
    """Base class for every failure raised by this module."""


class LLMConfigError(LLMError):
    """Bad or missing configuration (unknown backend, missing key, bad effort)."""


class LLMAuthError(LLMError):
    """The backend rejected our credentials or is not logged in."""


class LLMRateLimitError(LLMError):
    """Rate limited; safe to retry later."""


class LLMBackendUnavailable(LLMError):
    """Network down, server error, timeout, or the CLI binary is missing."""


class LLMSchemaError(LLMError):
    """The model's output was truncated, unparseable, or failed schema validation."""


class LLMRefusal(LLMError):
    """The model declined the request (stop_reason == 'refusal')."""


class LLMBudgetExceeded(LLMError):
    """The cli backend hit --max-budget-usd before finishing."""


@dataclass
class LLMResult:
    data: dict[str, Any]
    backend: str
    model: str
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None
    session_id: str | None = None
    raw: Any = field(default=None, repr=False)


# --- configuration ----------------------------------------------------------


def _env(name: str, default: str | None = None) -> str | None:
    """os.environ lookup that treats an empty value (e.g. `LLM_CLI_PATH=`) as unset."""
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def get_backend() -> str:
    backend = (_env("LLM_BACKEND", DEFAULT_BACKEND) or DEFAULT_BACKEND).lower()
    if backend not in VALID_BACKENDS:
        raise LLMConfigError(f"LLM_BACKEND={backend!r}; expected one of {VALID_BACKENDS}")
    return backend


def _resolve_settings(
    model: str | None,
    effort: str | None,
    timeout_s: float | None,
    max_tokens: int | None,
) -> tuple[str, str, float, int]:
    model = model or _env("LLM_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL
    effort = (effort or _env("LLM_EFFORT", DEFAULT_EFFORT) or DEFAULT_EFFORT).lower()
    if effort not in VALID_EFFORTS:
        raise LLMConfigError(f"LLM_EFFORT={effort!r}; expected one of {VALID_EFFORTS}")
    if timeout_s is None:
        raw_timeout = _env("LLM_TIMEOUT_S", str(DEFAULT_TIMEOUT_S)) or str(DEFAULT_TIMEOUT_S)
        try:
            timeout_s = float(raw_timeout)
        except ValueError as e:
            raise LLMConfigError(f"LLM_TIMEOUT_S={raw_timeout!r} is not a number") from e
    return model, effort, timeout_s, max_tokens or DEFAULT_MAX_TOKENS


def _validate_schema(schema: Any) -> None:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise LLMConfigError("schema must be a JSON Schema dict with \"type\": \"object\"")


def _check_required(data: dict[str, Any], schema: dict[str, Any]) -> None:
    """Root-level sanity check; both backends validate server-side, this catches drift."""
    missing = [key for key in schema.get("required", []) if key not in data]
    if missing:
        raise LLMSchemaError(f"Model output is missing required keys: {missing}")


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMSchemaError(f"Model output was not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise LLMSchemaError("Model output was JSON but not an object")
    return data


# --- public API --------------------------------------------------------------


def complete_structured(
    system: str,
    user: str,
    schema: dict[str, Any],
    *,
    model: str | None = None,
    effort: str | None = None,
    timeout_s: float | None = None,
    max_tokens: int | None = None,
    tool_name: str = DEFAULT_TOOL_NAME,
    tool_description: str | None = None,
) -> LLMResult:
    """
    Send one system + user prompt to Claude and get back a dict that conforms
    to `schema` (a JSON Schema object with "type": "object"). Which backend
    runs is decided by LLM_BACKEND; the caller never sees the difference.

    `tool_name` / `tool_description` name the strict tool the model calls in
    the api backend's tool mode (ignored elsewhere). The system prompt should
    tell the model to call that tool once with its answer.

    Raises a subclass of LLMError on any failure.
    """
    _validate_schema(schema)
    backend = get_backend()
    model, effort, timeout_s, max_tokens = _resolve_settings(model, effort, timeout_s, max_tokens)

    if backend == "api":
        result = _api_complete(
            system, user, schema, model=model, effort=effort, timeout_s=timeout_s, max_tokens=max_tokens,
            tool_name=tool_name, tool_description=tool_description,
        )
    else:
        result = _cli_complete(system, user, schema, model=model, effort=effort, timeout_s=timeout_s)

    _check_required(result.data, schema)
    logger.info(
        "llm_client: backend=%s model=%s usage=%s cost_usd=%s",
        result.backend, result.model, result.usage, result.cost_usd,
    )
    return result


def check_backend() -> dict[str, Any]:
    """
    Token-free readiness probe for the configured backend. Returns a dict with
    at least {"backend", "model", "ok"}; raises LLMError when the backend
    cannot be reached at all.
    """
    backend = get_backend()
    model = _env("LLM_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL
    info: dict[str, Any] = {"backend": backend, "model": model, "ok": False}

    if backend == "api":
        client = _api_client(timeout_s=30.0)
        try:
            found = client.models.retrieve(model)
        except anthropic.AuthenticationError as e:
            raise LLMAuthError(f"Anthropic rejected ANTHROPIC_API_KEY: {e.message}") from e
        except anthropic.NotFoundError as e:
            raise LLMConfigError(f"LLM_MODEL={model!r} is not a known model: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise LLMBackendUnavailable(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMBackendUnavailable(f"Could not reach the Anthropic API: {e.message}") from e
        info.update(ok=True, model=found.id, sdk_version=anthropic.__version__)
        return info

    cli = _resolve_cli()
    version = _run_cli_quick([cli, "--version"]).strip()
    status_raw = _run_cli_quick([cli, "auth", "status", "--json"], allow_failure=True)
    try:
        status = json.loads(status_raw) if status_raw.strip() else {}
    except json.JSONDecodeError:
        status = {}
    logged_in = bool(status.get("loggedIn"))
    info.update(
        ok=logged_in,
        cli_path=cli,
        cli_version=version,
        logged_in=logged_in,
        auth_method=status.get("authMethod"),
    )
    if not logged_in:
        info["hint"] = (
            "CLI is not logged in. Run `claude auth login`, or on a headless host set "
            "CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`."
        )
    return info


# --- api backend -------------------------------------------------------------


def _api_client(timeout_s: float):
    if anthropic is None:
        raise LLMBackendUnavailable(
            "The anthropic SDK is not installed; run: pip install -r backend/requirements.txt"
        )
    api_key = _env("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMConfigError("ANTHROPIC_API_KEY is not set in .env (required for LLM_BACKEND=api)")
    return anthropic.Anthropic(api_key=api_key, timeout=timeout_s)


def _api_output_mode(model: str) -> str:
    mode = (_env("LLM_API_OUTPUT_MODE", DEFAULT_API_OUTPUT_MODE) or DEFAULT_API_OUTPUT_MODE).lower()
    if mode not in VALID_API_OUTPUT_MODES:
        raise LLMConfigError(f"LLM_API_OUTPUT_MODE={mode!r}; expected one of {VALID_API_OUTPUT_MODES}")
    if model.startswith(_FORMAT_ONLY_MODEL_PREFIXES):
        return "format"
    return mode


def _tool_definition(schema: dict[str, Any], tool_name: str, tool_description: str | None) -> dict[str, Any]:
    return {
        "name": tool_name,
        "description": tool_description or "Return the result. Call exactly once with the complete answer.",
        "strict": True,
        "input_schema": schema,
    }


def _api_complete(
    system: str,
    user: str,
    schema: dict[str, Any],
    *,
    model: str,
    effort: str,
    timeout_s: float,
    max_tokens: int,
    tool_name: str = DEFAULT_TOOL_NAME,
    tool_description: str | None = None,
) -> LLMResult:
    client = _api_client(timeout_s)
    mode = _api_output_mode(model)
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        # Thinking is left at the model's adaptive default; effort steers depth.
        "output_config": {"effort": effort},
    }
    if mode == "tool":
        request["tools"] = [_tool_definition(schema, tool_name, tool_description)]
        request["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
    else:
        request["output_config"]["format"] = {"type": "json_schema", "schema": schema}

    try:
        if ENABLE_REFUSAL_FALLBACKS:
            response = client.beta.messages.create(betas=[_FALLBACK_BETA], fallbacks="default", **request)
        else:
            response = client.messages.create(**request)
    except anthropic.AuthenticationError as e:
        raise LLMAuthError(f"Anthropic rejected ANTHROPIC_API_KEY: {e.message}") from e
    except anthropic.PermissionDeniedError as e:
        raise LLMAuthError(f"ANTHROPIC_API_KEY lacks permission: {e.message}") from e
    except anthropic.RateLimitError as e:
        raise LLMRateLimitError(f"Anthropic rate limit: {e.message}") from e
    except anthropic.APIStatusError as e:
        if e.status_code >= 500:
            raise LLMBackendUnavailable(f"Anthropic server error {e.status_code}: {e.message}") from e
        raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:  # includes APITimeoutError
        raise LLMBackendUnavailable(f"Could not reach the Anthropic API: {e.message}") from e

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        explanation = getattr(details, "explanation", None)
        raise LLMRefusal(f"Model refused the request (category={category!r}): {explanation}")
    if response.stop_reason == "max_tokens":
        raise LLMSchemaError(f"Output truncated at max_tokens={max_tokens}; raise max_tokens and retry")

    if mode == "tool":
        call = next(
            (b for b in response.content if getattr(b, "type", None) == "tool_use" and b.name == tool_name),
            None,
        )
        if call is None:
            text = " ".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
            raise LLMSchemaError(
                f"Model did not call the {tool_name!r} tool; it answered in text instead: {text[:300]!r}"
            )
        data = call.input
        if isinstance(data, str):  # defensive: some SDK paths leave input serialized
            data = _parse_json_object(data)
        if not isinstance(data, dict):
            raise LLMSchemaError("Tool input was not a JSON object")
    else:
        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
        if text is None:
            raise LLMSchemaError("Response contained no text block")
        data = _parse_json_object(text)

    usage = response.usage.to_dict() if hasattr(response.usage, "to_dict") else None
    return LLMResult(data=data, backend="api", model=str(response.model), usage=usage, raw=response)


def estimate_tokens(
    system: str,
    user: str,
    schema: dict[str, Any],
    *,
    model: str | None = None,
    tool_name: str = DEFAULT_TOOL_NAME,
    tool_description: str | None = None,
) -> tuple[int, str]:
    """
    Input-token estimate for a complete_structured() call, without generating
    anything. Uses the free count_tokens endpoint when an API key is present
    (regardless of backend), else a chars/4 heuristic. Returns (tokens, method).
    """
    model = model or _env("LLM_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL
    if anthropic is not None and _env("ANTHROPIC_API_KEY"):
        try:
            client = _api_client(timeout_s=30.0)
            request: dict[str, Any] = {
                "model": model,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            if _api_output_mode(model) == "tool":
                request["tools"] = [_tool_definition(schema, tool_name, tool_description)]
            counted = client.messages.count_tokens(**request)
            return int(counted.input_tokens), "count_tokens"
        except (LLMError, anthropic.APIError) as exc:  # fall through to the heuristic
            logger.warning("count_tokens unavailable (%s); using heuristic", exc)
    approx = (len(system) + len(user) + len(json.dumps(schema))) // 4
    return approx, "heuristic"


# --- cli backend -------------------------------------------------------------


def _resolve_cli() -> str:
    configured = _env("LLM_CLI_PATH")
    if configured:
        if not Path(configured).is_file():
            raise LLMBackendUnavailable(f"LLM_CLI_PATH={configured!r} does not exist")
        return configured
    found = shutil.which("claude")
    if not found:
        raise LLMBackendUnavailable(
            "`claude` was not found on PATH. Install the Claude Code CLI "
            "(curl -fsSL https://claude.ai/install.sh | bash) or set LLM_CLI_PATH in .env"
        )
    return found


def _cli_env() -> dict[str, str]:
    """
    Subprocess environment for the CLI: API-key variables and Claude Code
    session variables removed, CLAUDE_CODE_OAUTH_TOKEN / CLAUDE_CONFIG_DIR kept
    (see module docstring).
    """
    return {
        key: value
        for key, value in os.environ.items()
        if key in _CLI_KEEP or not (key in _CLI_SCRUB_EXACT or key.startswith(_CLI_SCRUB_PREFIX))
    }


def _cli_workdir() -> Path:
    """An empty directory, so the CLI never auto-discovers a CLAUDE.md or project settings."""
    workdir = Path(tempfile.gettempdir()) / "calendar_optimizer_claude_cwd"
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def _cli_command(cli: str, system: str, user: str, schema: dict[str, Any], *, model: str, effort: str) -> list[str]:
    budget = _env("LLM_CLI_MAX_BUDGET_USD", DEFAULT_CLI_MAX_BUDGET_USD) or DEFAULT_CLI_MAX_BUDGET_USD
    return [
        cli,
        "-p", user,
        "--output-format", "json",
        "--json-schema", json.dumps(schema, separators=(",", ":")),
        "--system-prompt", system,
        "--model", model,
        "--effort", effort,
        "--tools", "",                 # no built-in tools: one shot, no agent loop
        "--no-session-persistence",    # nothing written to ~/.claude/projects
        "--strict-mcp-config",         # with no --mcp-config: zero MCP servers
        "--max-budget-usd", budget,
        # NOTE: --bare is intentionally absent; it disables subscription auth.
    ]


def _run_cli_quick(cmd: list[str], *, allow_failure: bool = False, timeout_s: float = 30.0) -> str:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(_cli_workdir()),
            env=_cli_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except OSError as e:  # missing binary, no permission, argument list too long
        raise LLMBackendUnavailable(f"Could not execute {cmd[0]!r}: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise LLMBackendUnavailable(f"{' '.join(cmd[:3])} timed out after {timeout_s}s") from e
    if proc.returncode != 0 and not allow_failure:
        raise LLMBackendUnavailable(
            f"{' '.join(cmd[1:3])} failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[:500]}"
        )
    return proc.stdout


def _classify_cli_failure(proc: subprocess.CompletedProcess, payload: dict[str, Any] | None) -> LLMError:
    subtype = str((payload or {}).get("subtype") or "")
    message = (
        (payload or {}).get("result")
        or (proc.stderr or "").strip()
        or (proc.stdout or "").strip()[:500]
        or f"exit code {proc.returncode}"
    )
    lowered = f"{subtype} {message}".lower()
    label = subtype or f"exit {proc.returncode}"

    if "structured_output" in subtype:
        return LLMSchemaError(f"claude CLI could not produce schema-valid output ({label}): {message}")
    if "budget" in lowered:
        return LLMBudgetExceeded(f"claude CLI stopped at --max-budget-usd ({label}): {message}")
    if any(token in lowered for token in ("not logged in", "login", "authentication", "unauthorized", "oauth")):
        return LLMAuthError(f"claude CLI is not authenticated ({label}): {message}")
    if "rate limit" in lowered or "429" in lowered or "usage limit" in lowered:
        return LLMRateLimitError(f"claude CLI rate/usage limit ({label}): {message}")
    return LLMError(f"claude CLI failed ({label}): {message}")


def _parse_cli_payload(proc: subprocess.CompletedProcess) -> dict[str, Any] | None:
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        # Tolerate a stray log line before the result object.
        try:
            payload = json.loads(stdout.splitlines()[-1])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def _cli_complete(
    system: str,
    user: str,
    schema: dict[str, Any],
    *,
    model: str,
    effort: str,
    timeout_s: float,
) -> LLMResult:
    if len(system) + len(user) > _MAX_CLI_PROMPT_CHARS:
        raise LLMError(
            f"Prompt is {len(system) + len(user)} chars; the cli backend caps at "
            f"{_MAX_CLI_PROMPT_CHARS} (argv limits). Trim the input or use LLM_BACKEND=api."
        )
    cli = _resolve_cli()
    cmd = _cli_command(cli, system, user, schema, model=model, effort=effort)

    with _CLI_LOCK:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(_cli_workdir()),
                env=_cli_env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except OSError as e:  # missing binary, no permission, argument list too long
            raise LLMBackendUnavailable(f"Could not execute {cli!r}: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise LLMBackendUnavailable(f"claude CLI timed out after {timeout_s}s") from e

    payload = _parse_cli_payload(proc)
    if payload is None:
        if proc.returncode != 0:
            raise _classify_cli_failure(proc, None)
        raise LLMSchemaError(f"claude CLI printed no JSON result: {(proc.stdout or '').strip()[:500]}")
    if proc.returncode != 0 or payload.get("is_error") or payload.get("subtype") != "success":
        raise _classify_cli_failure(proc, payload)

    data = payload.get("structured_output")
    if data is None:
        # Older CLIs put the JSON text in `result`; newer ones populate structured_output.
        data = _parse_json_object(str(payload.get("result") or ""))
    if not isinstance(data, dict):
        raise LLMSchemaError("claude CLI structured_output was not a JSON object")

    model_usage = payload.get("modelUsage") or {}
    served_model = next(iter(model_usage), None) or model
    cost = payload.get("total_cost_usd")
    return LLMResult(
        data=data,
        backend="cli",
        model=str(served_model),
        usage=payload.get("usage"),
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        session_id=payload.get("session_id"),
        raw=payload,
    )


# --- CLI entry point ---------------------------------------------------------


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if "--check" not in sys.argv:
        print("Usage: python3 llm_client.py --check   # token-free readiness probe", file=sys.stderr)
        sys.exit(2)
    try:
        report = check_backend()
    except LLMError as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}, indent=2))
        sys.exit(1)
    print(json.dumps(report, indent=2))
    sys.exit(0 if report.get("ok") else 1)
