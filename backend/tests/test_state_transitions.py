"""상태 전이 표(docs/05) + REGISTER_ACK 잡 규칙 + PATCH /state 검증."""

from __future__ import annotations

import pytest

from app.constants import STATE_TRANSITIONS, DeviceState
from app.modules.device.schemas import StatePatch
from app.mqtt.config_sync import register_ack_job_for

U = "20363930594D50170004003A"


def test_transition_table_matches_docs():
    assert STATE_TRANSITIONS["PENDING"] == {"ACTIVE", "REJECTED", "RETIRED"}
    assert STATE_TRANSITIONS["ACTIVE"] == {"SUSPENDED", "RETIRED", "PENDING"}
    assert STATE_TRANSITIONS["SUSPENDED"] == {"ACTIVE", "RETIRED"}
    assert STATE_TRANSITIONS["REJECTED"] == {"PENDING", "RETIRED"}
    assert STATE_TRANSITIONS["RETIRED"] == {"PENDING"}
    assert set(STATE_TRANSITIONS) == {s.value for s in DeviceState}


def test_retired_to_active_needs_pending():
    assert "ACTIVE" not in STATE_TRANSITIONS["RETIRED"]
    assert "ACTIVE" in STATE_TRANSITIONS["PENDING"]


def test_same_state_is_not_a_transition():
    for state, targets in STATE_TRANSITIONS.items():
        assert state not in targets


def test_state_patch_normalizes_and_validates():
    assert StatePatch(state="active").state == "ACTIVE"
    assert StatePatch(state=" retired ", site="A-12").site == "A-12"
    with pytest.raises(ValueError):
        StatePatch(state="DELETED")
    with pytest.raises(ValueError):
        StatePatch(state="ACTIVE", site="x" * 25)   # 24자 초과


def test_register_ack_job_rules():
    job = register_ack_job_for(uuid=U, state="ACTIVE", site="A-12", reason="memo")
    assert (job.state, job.site, job.reason, job.clear_after) == ("ACTIVE", "A-12", None, False)
    job = register_ack_job_for(uuid=U, state="REJECTED", site=None, reason="unknown device")
    assert (job.reason, job.clear_after) == ("unknown device", False)
    job = register_ack_job_for(uuid=U, state="RETIRED", site="A-12", reason=None)
    assert job.clear_after is True
    job = register_ack_job_for(uuid=U, state="PENDING", site=None, reason=None)
    assert (job.state, job.clear_after) == ("PENDING", False)
