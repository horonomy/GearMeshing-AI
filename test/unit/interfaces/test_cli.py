"""Tests for the gmai operator CLI (GMAI-14)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from gearmeshing_ai import __version__
from gearmeshing_ai.adapters.agent_assembly_policy_gate import AgentAssemblyPolicyGate
from gearmeshing_ai.application.ports.tool_policy import PolicyDecision
from gearmeshing_ai.domain.work_run import WorkRun, WorkRunCorrelation, WorkRunState
from gearmeshing_ai.interfaces import cli as cli_module
from gearmeshing_ai.interfaces.cli import app
from gearmeshing_ai.runtime.checkpoint_store import JsonFileCheckpointStore

runner = CliRunner()

_SECRET_TOKEN = "super-secret-jira-api-token-value"  # fixture credential, not a real secret

_JIRA_ENV = {
    "GMAI_JIRA_SITE_URL": "https://lightning-dust-mite.atlassian.net",
    "GMAI_JIRA_EMAIL": "operator@example.com",
    "GMAI_JIRA_API_TOKEN": _SECRET_TOKEN,
    "GMAI_JIRA_PROJECT_KEY": "GMAI",
    "GMAI_REPOSITORY_OWNER": "horonomy",
    "GMAI_REPOSITORY_NAME": "GearMeshing-AI",
    "GMAI_REPOSITORY_URL": "https://github.com/horonomy/GearMeshing-AI",
}

_NOW = "2026-07-24T12:00:00.000+0000"


def _correlation(jira_issue_key: str = "GMAI-14") -> WorkRunCorrelation:
    return WorkRunCorrelation(
        jira_issue_key=jira_issue_key,
        jira_issue_url=f"https://lightning-dust-mite.atlassian.net/browse/{jira_issue_key}",
        jira_issue_revision="10",
        jira_issue_content_sha256="a" * 64,
        repository_url="https://github.com/horonomy/GearMeshing-AI",
        branch_name="mvp1/GMAI-14/cli_work_run_controls",
        agent_assembly_run_id="assembly-run-14",
    )


def _seed(store_dir: Path, run: WorkRun) -> None:
    JsonFileCheckpointStore(store_dir).save(expected=None, updated=run)


def _approved(run_id: str = "GMAI-14") -> WorkRun:
    from datetime import UTC, datetime

    return WorkRun.approve(
        run_id=run_id, correlation=_correlation(run_id), actor_id="human-product-owner", occurred_at=datetime.now(UTC)
    )


def _advance(run: WorkRun, *states: WorkRunState) -> WorkRun:
    from datetime import UTC, datetime

    current = run
    for state in states:
        current = current.transition_to(state, actor_id="agent-assembly", occurred_at=datetime.now(UTC))
    return current


def _issue_payload(
    *,
    key: str = "GMAI-14",
    ready: bool = True,
    issue_type: str = "Story",
) -> dict[str, object]:
    description: dict[str, object] = {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Approved specification"}]},
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Acceptance Criteria"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "It works"}]},
        ],
    }
    return {
        "key": key,
        "fields": {
            "summary": "Provide CLI controls for POC work runs",
            "description": description,
            "status": {"name": "In Progress"},
            "labels": ["spec-ready"] if ready else [],
            "issuetype": {"name": issue_type},
            "updated": _NOW,
        },
        "properties": {},
    }


type Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def mock_jira_transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[Handler], None]]:
    """Redirect the CLI's Jira adapter onto an in-process mock transport.

    ``gmai run`` builds its own ``httpx.AsyncClient`` internally (there is no
    production wiring yet to inject one), so the only seam available to
    tests is the ``httpx.AsyncClient`` constructor imported by the Jira
    adapter module.
    """
    installed: list[Handler] = []

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            handler = installed[-1]
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("gearmeshing_ai.adapters.jira_work_management.httpx.AsyncClient", _PatchedAsyncClient)

    def _install(handler: Handler) -> None:
        installed.append(handler)

    yield _install


def test_version_command_prints_the_package_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_run_starts_a_ready_work_item_and_reaches_approved(
    tmp_path: Path, mock_jira_transport: Callable[[Handler], None]
) -> None:
    mock_jira_transport(lambda _request: httpx.Response(200, json=_issue_payload(ready=True)))

    result = runner.invoke(app, ["run", "gmai-14", "--checkpoints-dir", str(tmp_path)], env=_JIRA_ENV)

    assert result.exit_code == 0, result.stdout
    assert "started" in result.stdout
    persisted = JsonFileCheckpointStore(tmp_path).load("GMAI-14")
    assert persisted is not None
    assert persisted.state is WorkRunState.APPROVED


def test_run_blocks_a_not_ready_work_item(tmp_path: Path, mock_jira_transport: Callable[[Handler], None]) -> None:
    mock_jira_transport(lambda _request: httpx.Response(200, json=_issue_payload(ready=False)))

    result = runner.invoke(app, ["run", "GMAI-14", "--checkpoints-dir", str(tmp_path), "--json"], env=_JIRA_ENV)

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["state"] == "blocked"


def test_run_reports_an_already_started_work_run_without_calling_jira(
    tmp_path: Path, mock_jira_transport: Callable[[Handler], None]
) -> None:
    def _unexpected(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Jira must not be called for an already-started run")

    mock_jira_transport(_unexpected)
    _seed(tmp_path, _approved("GMAI-14"))

    result = runner.invoke(app, ["run", "GMAI-14", "--checkpoints-dir", str(tmp_path)], env=_JIRA_ENV)

    assert result.exit_code == 0, result.stdout
    assert "already exists" in result.stdout


class _FakeAssemblyContext:
    """Duck-typed stand-in for ``agent_assembly.AssemblyContext`` used only to
    prove the CLI calls ``shutdown()`` on exit; the policy-gate tests below
    monkeypatch ``AgentAssemblyPolicyGate`` directly rather than exercising a
    real ``GatewayClient``, since the CLI's own SDK-initialization call is
    covered separately by ``test_init_agent_assembly_*``.
    """

    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def test_init_agent_assembly_is_skipped_when_no_gateway_url_is_configured() -> None:
    assert cli_module._init_agent_assembly("gmai-cli-operator") is None


def test_init_agent_assembly_degrades_gracefully_on_a_failed_init(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_assembly import AssemblyError

    def _raise(**_kwargs: object) -> object:
        raise AssemblyError("boom")

    monkeypatch.setenv("AA_GATEWAY_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(cli_module, "init_assembly", _raise)

    assert cli_module._init_agent_assembly("gmai-cli-operator") is None


def test_run_calls_init_assembly_and_shuts_it_down_on_exit(
    tmp_path: Path, mock_jira_transport: Callable[[Handler], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_jira_transport(lambda _request: httpx.Response(200, json=_issue_payload(ready=True)))
    fake_context = _FakeAssemblyContext()
    monkeypatch.setattr(cli_module, "_init_agent_assembly", lambda _actor_id: fake_context)

    async def _allow(self: object, **_kwargs: object) -> PolicyDecision:
        return PolicyDecision(allowed=True, reason="observe mode")

    monkeypatch.setattr(AgentAssemblyPolicyGate, "check", _allow)

    result = runner.invoke(app, ["run", "GMAI-14", "--checkpoints-dir", str(tmp_path)], env=_JIRA_ENV)

    assert result.exit_code == 0, result.stdout
    assert fake_context.shutdown_calls == 1


def test_run_blocks_via_agent_assembly_policy_denial(
    tmp_path: Path, mock_jira_transport: Callable[[Handler], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unexpected(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Jira must not be called once the policy gate denies")

    mock_jira_transport(_unexpected)
    monkeypatch.setattr(cli_module, "_init_agent_assembly", lambda _actor_id: _FakeAssemblyContext())

    async def _deny(self: object, **_kwargs: object) -> PolicyDecision:
        return PolicyDecision(allowed=False, reason="egress not allow-listed")

    monkeypatch.setattr(AgentAssemblyPolicyGate, "check", _deny)

    result = runner.invoke(app, ["run", "GMAI-14", "--checkpoints-dir", str(tmp_path)], env=_JIRA_ENV)

    assert result.exit_code == 1
    assert "egress not allow-listed" in result.output
    assert JsonFileCheckpointStore(tmp_path).load("GMAI-14") is None


def test_run_fails_safely_when_jira_credentials_are_missing(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", "GMAI-14", "--checkpoints-dir", str(tmp_path)], env={})

    assert result.exit_code == 1
    assert "missing required environment variables" in result.output
    assert "Traceback" not in result.output


def test_run_output_never_contains_the_jira_api_token(
    tmp_path: Path, mock_jira_transport: Callable[[Handler], None]
) -> None:
    mock_jira_transport(lambda _request: httpx.Response(200, json=_issue_payload(ready=True)))

    result = runner.invoke(app, ["run", "GMAI-14", "--checkpoints-dir", str(tmp_path), "--json"], env=_JIRA_ENV)

    assert result.exit_code == 0, result.stdout
    assert _SECRET_TOKEN not in result.stdout
    persisted_raw = (tmp_path / "GMAI-14.json").read_text(encoding="utf-8")
    assert _SECRET_TOKEN not in persisted_raw


def test_run_error_output_never_contains_the_jira_api_token(tmp_path: Path) -> None:
    bad_env = dict(_JIRA_ENV)
    bad_env["GMAI_JIRA_SITE_URL"] = "https://this-host-does-not-resolve.invalid"

    result = runner.invoke(app, ["run", "GMAI-14", "--checkpoints-dir", str(tmp_path)], env=bad_env)

    assert result.exit_code == 1
    assert _SECRET_TOKEN not in result.output


def test_status_reports_a_persisted_work_run(tmp_path: Path) -> None:
    _seed(tmp_path, _approved("GMAI-14"))

    result = runner.invoke(app, ["status", "GMAI-14", "--checkpoints-dir", str(tmp_path)])

    assert result.exit_code == 0, result.stdout
    assert "state: approved" in result.stdout


def test_status_resolves_identifiers_case_insensitively(tmp_path: Path) -> None:
    _seed(tmp_path, _approved("GMAI-14"))

    result = runner.invoke(app, ["status", "gmai-14", "--checkpoints-dir", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["run_id"] == "GMAI-14"


def test_status_json_output_is_well_formed_and_machine_readable(tmp_path: Path) -> None:
    run = _advance(_approved("GMAI-14"), WorkRunState.EXECUTING)
    _seed(tmp_path, run)

    result = runner.invoke(app, ["status", "GMAI-14", "--checkpoints-dir", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["state"] == "executing"
    assert payload["run_id"] == "GMAI-14"
    assert len(payload["events"]) == 2


def test_status_fails_safely_for_an_unknown_identifier(tmp_path: Path) -> None:
    result = runner.invoke(app, ["status", "does-not-exist", "--checkpoints-dir", str(tmp_path)])

    assert result.exit_code == 1
    assert "no work run found" in result.output
    assert "Traceback" not in result.output


def test_status_fails_safely_for_a_blank_identifier(tmp_path: Path) -> None:
    result = runner.invoke(app, ["status", "   ", "--checkpoints-dir", str(tmp_path)])

    assert result.exit_code == 1
    assert "must not be blank" in result.output


def test_status_json_error_output_is_well_formed(tmp_path: Path) -> None:
    result = runner.invoke(app, ["status", "does-not-exist", "--checkpoints-dir", str(tmp_path), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["command"] == "status"


def test_cancel_transitions_an_active_run_to_cancelled(tmp_path: Path) -> None:
    run = _advance(_approved("GMAI-14"), WorkRunState.EXECUTING)
    _seed(tmp_path, run)

    result = runner.invoke(app, ["cancel", "GMAI-14", "--checkpoints-dir", str(tmp_path)])

    assert result.exit_code == 0, result.stdout
    assert "cancelled" in result.stdout
    persisted = JsonFileCheckpointStore(tmp_path).load("GMAI-14")
    assert persisted is not None
    assert persisted.state is WorkRunState.CANCELLED
    assert persisted.events[-1].name == "entered_cancelled"


def test_cancel_records_the_cancellation_as_a_workflow_event(tmp_path: Path) -> None:
    run = _advance(_approved("GMAI-14"), WorkRunState.EXECUTING)
    _seed(tmp_path, run)

    runner.invoke(app, ["cancel", "GMAI-14", "--checkpoints-dir", str(tmp_path), "--actor-id", "operator-jane"])

    persisted = JsonFileCheckpointStore(tmp_path).load("GMAI-14")
    assert persisted is not None
    latest = persisted.events[-1]
    assert latest.state is WorkRunState.CANCELLED
    assert latest.actor_id == "operator-jane"


def test_cancel_fails_safely_on_an_already_terminal_run(tmp_path: Path) -> None:
    run = _advance(_approved("GMAI-14"), WorkRunState.EXECUTING, WorkRunState.CANCELLED)
    _seed(tmp_path, run)

    result = runner.invoke(app, ["cancel", "GMAI-14", "--checkpoints-dir", str(tmp_path)])

    assert result.exit_code == 1
    assert "cannot cancel" in result.output
    assert "Traceback" not in result.output
    persisted = JsonFileCheckpointStore(tmp_path).load("GMAI-14")
    assert persisted == run


def test_cancel_fails_safely_for_an_unknown_run(tmp_path: Path) -> None:
    result = runner.invoke(app, ["cancel", "does-not-exist", "--checkpoints-dir", str(tmp_path)])

    assert result.exit_code == 1
    assert "no work run found" in result.output


def test_retry_moves_a_remediating_run_back_to_verifying(tmp_path: Path) -> None:
    run = _advance(
        _approved("GMAI-14"),
        WorkRunState.EXECUTING,
        WorkRunState.VERIFYING,
        WorkRunState.REMEDIATING,
    )
    _seed(tmp_path, run)

    result = runner.invoke(app, ["retry", "GMAI-14", "--checkpoints-dir", str(tmp_path)])

    assert result.exit_code == 0, result.stdout
    persisted = JsonFileCheckpointStore(tmp_path).load("GMAI-14")
    assert persisted is not None
    assert persisted.state is WorkRunState.VERIFYING
    assert persisted.events[-1].name == "entered_verifying"


def test_retry_records_the_retry_as_a_workflow_event(tmp_path: Path) -> None:
    run = _advance(
        _approved("GMAI-14"),
        WorkRunState.EXECUTING,
        WorkRunState.VERIFYING,
        WorkRunState.REMEDIATING,
    )
    _seed(tmp_path, run)

    runner.invoke(app, ["retry", "GMAI-14", "--checkpoints-dir", str(tmp_path), "--actor-id", "operator-jane"])

    persisted = JsonFileCheckpointStore(tmp_path).load("GMAI-14")
    assert persisted is not None
    assert persisted.events[-1].actor_id == "operator-jane"


def test_retry_fails_safely_on_a_run_that_is_not_remediating(tmp_path: Path) -> None:
    """``approved`` cannot transition directly to ``verifying``, unlike ``executing``."""
    run = _approved("GMAI-14")
    _seed(tmp_path, run)

    result = runner.invoke(app, ["retry", "GMAI-14", "--checkpoints-dir", str(tmp_path)])

    assert result.exit_code == 1
    assert "cannot retry" in result.output
    assert "Traceback" not in result.output


def test_retry_fails_safely_on_a_terminal_run(tmp_path: Path) -> None:
    run = _advance(_approved("GMAI-14"), WorkRunState.EXECUTING, WorkRunState.CANCELLED)
    _seed(tmp_path, run)

    result = runner.invoke(app, ["retry", "GMAI-14", "--checkpoints-dir", str(tmp_path)])

    assert result.exit_code == 1
    assert "cannot retry" in result.output


def test_retry_fails_safely_for_an_unknown_run(tmp_path: Path) -> None:
    result = runner.invoke(app, ["retry", "does-not-exist", "--checkpoints-dir", str(tmp_path)])

    assert result.exit_code == 1
    assert "no work run found" in result.output


def test_doctor_reports_ok_when_configuration_is_complete(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--checkpoints-dir", str(tmp_path)], env=_JIRA_ENV)

    assert result.exit_code == 0, result.stdout
    assert "[OK] jira_credentials" in result.stdout
    assert "[OK] repository_configuration" in result.stdout
    assert "[OK] checkpoints_directory" in result.stdout


def test_doctor_reports_missing_environment_variables(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--checkpoints-dir", str(tmp_path)], env={})

    assert result.exit_code == 1
    assert "[FAIL] jira_credentials" in result.output
    assert "GMAI_JIRA_API_TOKEN" in result.output


def test_doctor_json_output_is_machine_readable(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--checkpoints-dir", str(tmp_path), "--json"], env=_JIRA_ENV)

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert {check["name"] for check in payload["checks"]} == {
        "jira_credentials",
        "repository_configuration",
        "checkpoints_directory",
    }


def test_doctor_never_prints_the_jira_api_token(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--checkpoints-dir", str(tmp_path)], env=_JIRA_ENV)

    assert _SECRET_TOKEN not in result.output


def test_doctor_reports_failure_for_an_unwritable_checkpoints_directory(tmp_path: Path) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied", encoding="utf-8")

    result = runner.invoke(app, ["doctor", "--checkpoints-dir", str(blocked)], env=_JIRA_ENV)

    assert result.exit_code == 1
    assert "[FAIL] checkpoints_directory" in result.output
