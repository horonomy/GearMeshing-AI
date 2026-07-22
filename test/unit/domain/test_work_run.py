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
