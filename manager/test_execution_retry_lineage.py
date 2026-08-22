"""Authoritative retry lineage used by D2 reusable completion."""

import pytest

from manager.execution_runner import _authorized_retry_predecessor_execution_ids
from manager.tasks import TaskError


BRANCH = "refs/heads/feat/p1/t1"


class Store:
    def __init__(self, records):
        self.records = records

    def get(self, kind, project_id, execution_id):
        assert kind == "executions"
        return self.records[(project_id, execution_id)]


def _execution(execution_id, *, task_id="t1", retry_count=0, retry_of_execution_id=None, branch=BRANCH,
               status="failed"):
    return {
        "execution_id": execution_id, "project_id": "p1", "task_id": task_id,
        "retry_count": retry_count, "retry_of_execution_id": retry_of_execution_id,
        "lease_evidence": {"branch": branch}, "status": status,
    }


def _lineage(monkeypatch, current, *prior):
    monkeypatch.setattr("manager.execution_runner.validate", lambda *_args: None)
    records = {("p1", entry["execution_id"]): entry for entry in prior}
    return _authorized_retry_predecessor_execution_ids(Store(records), current)


def test_direct_retry_lineage_accepts_exact_persisted_predecessor(monkeypatch):
    assert _lineage(monkeypatch, _execution("e2", retry_count=1, retry_of_execution_id="e1", status="running"),
                    _execution("e1")) == frozenset({"e1"})


@pytest.mark.parametrize("prior", [
    _execution("e1", status="completed"),
    _execution("e1", task_id="other-task"),
    _execution("e1", branch="refs/heads/feat/p1/other"),
])
def test_spoofed_or_cross_scope_retry_lineage_is_rejected(monkeypatch, prior):
    current = _execution("e2", retry_count=1, retry_of_execution_id="e1", status="running")
    with pytest.raises(TaskError):
        _lineage(monkeypatch, current, prior)


def test_retry_lineage_missing_execution_record_is_rejected(monkeypatch):
    monkeypatch.setattr("manager.execution_runner.validate", lambda *_args: None)
    current = _execution("e2", retry_count=1, retry_of_execution_id="missing", status="running")
    with pytest.raises(TaskError):
        _authorized_retry_predecessor_execution_ids(Store({}), current)


def test_retry_lineage_must_decrement_through_every_predecessor(monkeypatch):
    current = _execution("e3", retry_count=2, retry_of_execution_id="e2", status="running")
    e2 = _execution("e2", retry_count=1, retry_of_execution_id="e1")
    e1 = _execution("e1")
    assert _lineage(monkeypatch, current, e2, e1) == frozenset({"e1", "e2"})
