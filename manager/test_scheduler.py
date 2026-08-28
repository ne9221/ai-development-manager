import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from manager.scheduler import schedule
from manager.tasks import TaskError, create_project, create_task


NOW = datetime(2026, 8, 9, 5, 0, tzinfo=timezone.utc)


class MemoryStore:
    def __init__(self): self.records = {}
    def put(self, area, project, name, document): self.records[(area, project, name)] = deepcopy(document); return document
    def get(self, area, project, name):
        if (area, project, name) not in self.records: raise TaskError("not found")
        return deepcopy(self.records[(area, project, name)])


def project():
    return {"project_id": "p1", "name": "Project", "repo": "https://github.com/example/project", "default_branch": "main", "working_directory": "C:/work/main", "baseline_commit": "abc", "runtime_ssot": "Drive", "project_rules": [], "active_tasks": [], "current_phase": "8", "important_constraints": []}


def task(task_id, **changes):
    value = {"task_id": task_id, "project_id": "p1", "title": task_id, "status": "ready", "task_type": "implementation", "complexity": "medium", "expected_minutes": 20, "needs_repo_edit": True, "scope": [task_id], "constraints": [], "acceptance_criteria": [], "depends_on": [], "allowed_paths": [f"src/{task_id}.py"], "read_only": False, "worktree_id": task_id, "working_directory": f"C:/work/{task_id}"}
    value.update(changes); return value


def quota(codex=80, claude=80, updated=NOW, reset=None):
    providers = []
    for name, remaining in (("codex", codex), ("claude", claude), ("antigravity", None), ("gemini_app", None)):
        windows = [] if remaining is None else [{"name": "primary", "remaining_percent": remaining, "used_percent": 100-remaining, "resets_at": reset}]
        providers.append({"provider": name, "display_name": name, "collection_mode": "automatic" if name in ("codex", "claude") else "manual", "source": "test", "source_type": "official" if name in ("codex", "claude") else "manual", "confidence": "official" if windows else "unknown", "last_updated": updated.isoformat(), "status": "ok" if windows else "unknown", "windows": windows})
    return {"schema_version": "0.1.0", "generated_at": updated.isoformat(), "providers": providers}


def fake_dispatch(store, service, request, quota_document, history):
    defaults = {"implementation": "codex", "architecture": "claude", "research": "claude", "documentation": "gemini_app"}
    provider = request.get("preferred_provider") or defaults.get(request["task_type"], "codex")
    if provider == request.get("excluded_provider"):
        provider = "claude" if provider == "codex" else "codex"
    alternatives = [item for item in ("codex", "claude", "antigravity", "gemini_app") if item != provider and item != request.get("excluded_provider")]
    split = request["expected_minutes"] > 20
    return {"recommended_provider": provider, "mode": "code" if provider == "codex" else "analysis", "effort": "medium", "estimated_minutes": request["expected_minutes"], "split_recommended": split, "phase_count": 3 if split else 1, "alternatives": alternatives, "quota_summary": "short", "warnings": [], "generated_prompt": f"dispatcher:{request['task_id']}:{provider}"}


def fake_dispatch_with_waiting_quota(waiting_task_id):
    """Reproduces the real manager.dispatcher.dispatch() waiting_quota
    contract (recommended_provider=None, no generated_prompt) for one named
    task, falling back to the normal fake_dispatch() for every other task in
    the same batch."""
    def inner(store, service, request, quota_document, history):
        if request["task_id"] == waiting_task_id:
            return {"recommended_provider": None, "provider": None, "mode": None, "effort": "medium",
                    "estimated_minutes": None, "split_recommended": None, "phase_count": None,
                    "alternatives": [], "quota_summary": "no provider has usable quota",
                    "warnings": ["Task admitted; automatic provider selection is waiting on quota recovery "
                                 "(no provider has fresh reliable quota)"],
                    "generated_prompt": None, "waiting_quota": True}
        return fake_dispatch(store, service, request, quota_document, history)
    return inner


class SchedulerTests(unittest.TestCase):
    def setUp(self): self.store = MemoryStore(); create_project(self.store, project())

    def make(self, *tasks):
        records = [create_task(self.store, item, assign=False) for item in tasks]
        return schedule(self.store, object(), "p1", records, quota(), [], NOW, fake_dispatch)

    def test_independent_different_provider_tasks_parallel_and_prompt_reused(self):
        result = self.make(task("edit"), task("review", task_type="architecture", needs_repo_edit=False, read_only=True))
        self.assertEqual(2, len(result["execution_batches"][0]["tasks"]))
        self.assertEqual({"codex", "claude"}, {item["recommended_provider"] for item in result["execution_batches"][0]["tasks"]})
        self.assertTrue(all(item["dispatcher_result"]["generated_prompt"].startswith("dispatcher:") for item in result["execution_batches"][0]["tasks"]))

    def test_dependency_is_sequential(self):
        result = self.make(task("a"), task("b", task_type="architecture", needs_repo_edit=False, read_only=True, depends_on=["a"]))
        self.assertEqual(2, len(result["execution_batches"])); self.assertIn("depends on", result["execution_batches"][1]["tasks"][0]["dependency_reason"])

    def test_same_tree_edit_conflict_and_separate_tree_parallel(self):
        same = self.make(task("a", working_directory="C:/same", worktree_id=None), task("b", task_type="architecture", working_directory="C:/same", worktree_id=None))
        self.assertEqual(2, len(same["execution_batches"])); self.assertIn("working tree", same["execution_batches"][1]["tasks"][0]["conflict_reason"])
        separate = self.make(task("c", working_directory="C:/one", worktree_id="one"), task("d", task_type="architecture", working_directory="C:/two", worktree_id="two"))
        self.assertEqual(2, len(separate["execution_batches"][0]["tasks"]))

    def test_overlapping_and_nonoverlapping_file_scope(self):
        overlap = self.make(task("a", allowed_paths=["src/core"]), task("b", task_type="architecture", allowed_paths=["src/core/file.py"]))
        self.assertEqual(2, len(overlap["execution_batches"])); self.assertIn("overlapping", overlap["execution_batches"][1]["tasks"][0]["conflict_reason"])
        clear = self.make(task("c", allowed_paths=["src/a"]), task("d", task_type="architecture", allowed_paths=["src/b"]))
        self.assertEqual(2, len(clear["execution_batches"][0]["tasks"]))

    def test_read_only_and_edit_parallel(self):
        result = self.make(task("edit", working_directory="C:/same"), task("read", task_type="architecture", working_directory="C:/same", needs_repo_edit=False, read_only=True))
        self.assertEqual(2, len(result["execution_batches"][0]["tasks"]))

    def test_same_provider_capacity_creates_next_batch(self):
        result = self.make(task("a"), task("b"))
        self.assertEqual(2, len(result["execution_batches"])); self.assertIn("provider codex", result["execution_batches"][1]["tasks"][0]["conflict_reason"])

    def test_unknown_stale_and_reset_defer(self):
        item = create_task(self.store, task("manual", preferred_provider="antigravity"), assign=False)
        unknown = schedule(self.store, object(), "p1", [item], quota(), [], NOW, fake_dispatch)
        self.assertTrue(any("unknown" in warning for warning in unknown["warnings"]))
        stale = schedule(self.store, object(), "p1", [item], quota(updated=NOW-timedelta(hours=2)), [], NOW, fake_dispatch)
        self.assertTrue(any("stale" in warning for warning in stale["warnings"]))
        low = create_task(self.store, task("low"), assign=False)
        reset = schedule(self.store, object(), "p1", [low], quota(codex=10, reset=(NOW+timedelta(minutes=10)).isoformat()), [], NOW, fake_dispatch)
        self.assertEqual("defer_until_reset", reset["deferred_tasks"][0]["recommendation"])

    def test_waiting_quota_task_does_not_crash_batch_and_is_deferred_with_reason(self):
        """DASHBOARD_TRUTH_CONNECTED gate 1: manager.dispatcher.dispatch() can
        now return recommended_provider=None (waiting_quota) instead of
        raising when no provider is currently eligible. Before this fix,
        reset_defer() unconditionally looked up quota["providers"] by
        result["recommended_provider"] -- a None provider made that lookup
        raise StopIteration, crashing the *entire* batch, including
        unrelated tasks that did have an eligible provider."""
        stuck = create_task(self.store, task("stuck"), assign=False)
        ok = create_task(self.store, task("ok", task_type="architecture", needs_repo_edit=False, read_only=True), assign=False)
        result = schedule(self.store, object(), "p1", [stuck, ok], quota(), [], NOW,
                          fake_dispatch_with_waiting_quota("stuck"))
        scheduled_ids = {item["task_id"] for batch in result["execution_batches"] for item in batch["tasks"]}
        self.assertIn("ok", scheduled_ids)
        self.assertNotIn("stuck", scheduled_ids)
        deferred_ids = {item["task_id"] for item in result["deferred_tasks"]}
        self.assertIn("stuck", deferred_ids)
        stuck_entry = next(item for item in result["deferred_tasks"] if item["task_id"] == "stuck")
        self.assertIn("waiting on quota recovery", stuck_entry["reason"])

    def test_split_preferred_and_excluded(self):
        big = self.make(task("big", expected_minutes=45, preferred_provider="claude"))
        item = big["execution_batches"][0]["tasks"][0]
        self.assertEqual("Phase 1", item["scheduled_unit"]); self.assertEqual(20, item["estimated_minutes"]); self.assertEqual("claude", item["recommended_provider"])
        excluded = self.make(task("excluded", excluded_provider="codex"))
        self.assertNotEqual("codex", excluded["execution_batches"][0]["tasks"][0]["recommended_provider"])

    def test_malformed_task_rejected(self):
        with self.assertRaises(TaskError): schedule(self.store, object(), "p1", [{"task_id": "bad"}], quota(), [], NOW, fake_dispatch)


if __name__ == "__main__": unittest.main()
