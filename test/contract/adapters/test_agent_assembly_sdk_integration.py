"""Contract tests exercising the real Agent Assembly SDK against a real local gateway.

Starts the ``aasm`` binary bundled by the ``agent-assembly[runtime]`` extra
(``aasm start --mode local --foreground``, see ``pyproject.toml``) on a free
loopback port for each test, then calls ``agent_assembly.init_assembly()``
against it for real — no HTTP/gRPC mocking. Skipped entirely when the bundled
``aasm`` binary is not resolvable, following the same
environment-availability-gating convention used elsewhere in this sprint
(``test_docker_sandbox_integration.py``, ``test_quality_checks_integration.py``).

What these tests do and do not exercise, disclosed honestly per GMAI-58's own
acceptance criteria:

* Real SDK init/teardown against a real gateway, real agent registration, and
  a real (if degraded) ``sdk-only`` policy-check path through
  ``AgentAssemblyIdentityResolver``/``AgentAssemblyPolicyGate`` are covered
  below and genuinely exercise the SDK's own code paths, not a mock.
* A registered ``AssemblyContext`` in ``sdk-only`` mode has no native
  ``RuntimeClient`` attached unless a separate ``aa-runtime`` sidecar process
  is also connected to the gateway's gRPC port and this process's native
  extension can reach its Unix domain socket. ``aa-runtime`` is not bundled
  by the ``agent-assembly[runtime]`` PyPI extra (only ``aasm``/``aa-gateway``
  are) — reaching a real policy *deny* decision requires building it from
  the SDK's own Rust source repository, which is out of scope for GMAI-58's
  ``sdk-only`` (no sidecar/eBPF) mode. That path was manually verified once
  during this ticket's development (a built ``aa-runtime`` connected to a
  local gateway did return a real ``deny`` for a `high-risk.yaml`-policy'd
  tool), but is not re-verified here as an automated contract test since it
  depends on infrastructure this environment does not reproducibly provide.
  ``AgentAssemblyPolicyGate``'s degraded-registered-but-no-runtime-client
  path (``PolicyDecision(allowed=True, details={"decision": "unavailable"}``)
  is what every test below actually observes when no sidecar is running,
  and the unit tests in ``test_agent_assembly_policy_gate.py`` cover the
  real deny-mapping logic with a duck-typed fake runtime client instead.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import agent_assembly
import httpx
import pytest
from agent_assembly import init_assembly

from gearmeshing_ai.adapters.agent_assembly_identity import AgentAssemblyIdentityResolver
from gearmeshing_ai.adapters.agent_assembly_policy_gate import AgentAssemblyPolicyGate
from gearmeshing_ai.application.ports.agent_identity import ActorRole


def _resolve_aasm_path() -> Path | None:
    bundled = Path(agent_assembly.__file__).parent / "bin" / "aasm"
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return bundled
    on_path = shutil.which("aasm")
    return Path(on_path) if on_path is not None else None


_AASM_PATH = _resolve_aasm_path()

pytestmark = pytest.mark.skipif(
    _AASM_PATH is None,
    reason="the aasm binary bundled by the agent-assembly[runtime] extra is not available",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def local_gateway() -> Iterator[tuple[str, str, str]]:
    """Start a real local ``aasm`` gateway process; yield ``(gateway_url, api_key, agent_id)``.

    Every ``aasm start`` invocation prints a freshly generated admin API key to
    stdout/stderr on startup (see the module's captured log parsing below);
    there is no way to pre-supply one for a brand-new local instance.

    Agent registrations persist in ``~/.aasm/local.db`` across separate
    ``aasm start`` invocations (it is not wiped per-process), so a fixed
    ``agent_id`` collides with a prior test run's registration
    ("gateway rejected registration: agent already registered"). The
    port picked for this gateway is unique per fixture invocation, so
    deriving ``agent_id`` from it keeps every test's registration fresh.
    """
    assert _AASM_PATH is not None
    port = _free_port()
    agent_id = f"gmai-contract-{port}"
    log_path = Path(f"/tmp/gmai-contract-aasm-{port}.log")
    process = subprocess.Popen(
        [str(_AASM_PATH), "start", "--mode", "local", "--foreground", "--port", str(port), "--no-dashboard"],
        stdout=log_path.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    gateway_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_healthz(gateway_url, timeout=10.0)
        api_key = _read_api_key(log_path)
        yield gateway_url, api_key, agent_id
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log_path.unlink(missing_ok=True)


def _wait_for_healthz(gateway_url: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{gateway_url}/healthz", timeout=0.5)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"local aasm gateway at {gateway_url} did not become healthy within {timeout}s")


def _read_api_key(log_path: Path, *, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    pattern = re.compile(r"\b(aa_[a-f0-9]+)\b")
    while time.monotonic() < deadline:
        contents = log_path.read_text(encoding="utf-8")
        match = pattern.search(contents)
        if match is not None:
            return match.group(1)
        time.sleep(0.1)
    raise RuntimeError(f"no admin API key found in {log_path} within {timeout}s")


def test_init_assembly_registers_against_a_real_gateway_and_tears_down_cleanly(
    local_gateway: tuple[str, str, str],
) -> None:
    gateway_url, api_key, agent_id = local_gateway

    context = init_assembly(
        gateway_url=gateway_url,
        api_key=api_key,
        agent_id=agent_id,
        mode="sdk-only",
        enforcement_mode="observe",
    )

    try:
        assert context.registered is True
        assert context.is_shutdown is False
    finally:
        context.shutdown()

    assert context.is_shutdown is True


async def test_identity_resolver_returns_a_real_gateway_registered_actor_id(
    local_gateway: tuple[str, str, str],
) -> None:
    gateway_url, api_key, agent_id = local_gateway
    context = init_assembly(
        gateway_url=gateway_url,
        api_key=api_key,
        agent_id=agent_id,
        mode="sdk-only",
        enforcement_mode="observe",
    )
    try:
        identity = await AgentAssemblyIdentityResolver(context).resolve(ActorRole.CODING_EXECUTION)
    finally:
        context.shutdown()

    assert identity.actor_id == f"{agent_id}.coding_execution"


async def test_policy_gate_discloses_the_degraded_sdk_only_unavailable_path(
    local_gateway: tuple[str, str, str],
) -> None:
    """``sdk-only`` mode with no ``aa-runtime`` sidecar has no native runtime client.

    Registration against the real gateway succeeds (``context.registered is
    True``), but with no native ``RuntimeClient`` attached the policy gate
    cannot obtain an authoritative decision and discloses that explicitly via
    ``details["decision"] == "unavailable"`` rather than silently allowing
    without saying why. See the module docstring for why the deny-mapping
    itself is covered by ``test_agent_assembly_policy_gate.py`` instead.
    """
    gateway_url, api_key, agent_id = local_gateway
    context = init_assembly(
        gateway_url=gateway_url,
        api_key=api_key,
        agent_id=agent_id,
        mode="sdk-only",
        enforcement_mode="observe",
    )
    try:
        decision = await AgentAssemblyPolicyGate(context).check(
            agent_id=agent_id, action_type="tool_call", tool_name="coding_executor"
        )
    finally:
        context.shutdown()

    assert decision.allowed is True
    assert decision.details["decision"] == "unavailable"
    assert decision.details["registered"] == "True"
