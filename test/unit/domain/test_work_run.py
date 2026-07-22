"""Tests for the governed WorkRun aggregate."""

from datetime import UTC, datetime, timedelta

import pytest

from gearmeshing_ai.domain.work_run import (
    InvalidTransitionError,
    WorkRun,
    WorkRunArtifact,
    WorkRunCorrelation,
    WorkRunState,
    WorkRunValidationError,
)

NOW = datetime(2026, 7, 22, 3, 0, tzinfo=UTC)


def _correlation() -> WorkRunCorrelation:
    return WorkRunCorrelation(
        jira_issue_key="GMAI-11",
        jira_issue_url="https://lightning-dust-mite.atlassian.net/browse/GMAI-11",
        repository_url="https://github.com/horonomy/GearMeshing-AI",
        branch_name="mvp1/GMAI-11/workrun_state_model",
        agent_assembly_run_id="assembly-run-11",
    )


def _approved() -> WorkRun:
    return WorkRun.approve(
        run_id="work-run-11",
        correlation=_correlation(),
        actor_id="human-product-owner",
        occurred_at=NOW,
    )


def _advance(run: WorkRun, *states: WorkRunState) -> WorkRun:
    current = run
    for offset, state in enumerate(states, start=1):
        current = current.transition_to(
            state,
            actor_id="agent-assembly",
            occurred_at=NOW + timedelta(minutes=offset),
        )
    return current


def test_approval_creates_correlated_audit_evidence() -> None:
    run = _approved()

    assert run.state is WorkRunState.APPROVED
    assert run.correlation.jira_issue_key == "GMAI-11"
    assert run.correlation.repository_url == "https://github.com/horonomy/GearMeshing-AI"
    assert run.events[0].name == "approved"
    assert run.events[0].actor_id == "human-product-owner"


def test_happy_path_completes_after_recording_a_draft_pr() -> None:
    publishing = _advance(
        _approved(),
        WorkRunState.EXECUTING,
        WorkRunState.VERIFYING,
        WorkRunState.PUBLISHING_DRAFT_PR,
    )
    published = publishing.record_draft_pr(
        "https://github.com/horonomy/GearMeshing-AI/pull/2",
        actor_id="coding-agent",
        occurred_at=NOW + timedelta(minutes=4),
    )

    completed = published.transition_to(
        WorkRunState.COMPLETED,
        actor_id="agent-assembly",
        occurred_at=NOW + timedelta(minutes=5),
    )

    assert completed.state is WorkRunState.COMPLETED
    assert completed.draft_pr_url == "https://github.com/horonomy/GearMeshing-AI/pull/2"
    assert tuple(event.sequence for event in completed.events) == tuple(range(1, 7))


def test_verification_can_cycle_through_remediation() -> None:
    run = _advance(
        _approved(),
        WorkRunState.EXECUTING,
        WorkRunState.VERIFYING,
        WorkRunState.REMEDIATING,
        WorkRunState.VERIFYING,
    )

    assert run.state is WorkRunState.VERIFYING
    assert [event.state for event in run.events[-3:]] == [
        WorkRunState.VERIFYING,
        WorkRunState.REMEDIATING,
        WorkRunState.VERIFYING,
    ]


def test_invalid_transition_is_rejected_without_changing_the_run() -> None:
    approved = _approved()

    with pytest.raises(InvalidTransitionError, match="approved to verifying"):
        approved.transition_to(
            WorkRunState.VERIFYING,
            actor_id="agent-assembly",
            occurred_at=NOW + timedelta(minutes=1),
        )

    assert approved.state is WorkRunState.APPROVED
    assert len(approved.events) == 1


@pytest.mark.parametrize(
    "terminal_state",
    [WorkRunState.FAILED, WorkRunState.BLOCKED, WorkRunState.CANCELLED],
)
def test_terminal_outcomes_cannot_be_restarted(terminal_state: WorkRunState) -> None:
    terminal = _approved().transition_to(
        terminal_state,
        actor_id="agent-assembly",
        occurred_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(InvalidTransitionError, match=f"{terminal_state.value} to executing"):
        terminal.transition_to(
            WorkRunState.EXECUTING,
            actor_id="agent-assembly",
            occurred_at=NOW + timedelta(minutes=2),
        )


def test_completion_without_a_draft_pr_is_rejected() -> None:
    publishing = _advance(
        _approved(),
        WorkRunState.EXECUTING,
        WorkRunState.VERIFYING,
        WorkRunState.PUBLISHING_DRAFT_PR,
    )

    with pytest.raises(WorkRunValidationError, match="requires a Draft PR URL"):
        publishing.transition_to(
            WorkRunState.COMPLETED,
            actor_id="agent-assembly",
            occurred_at=NOW + timedelta(minutes=4),
        )

    assert publishing.state is WorkRunState.PUBLISHING_DRAFT_PR
