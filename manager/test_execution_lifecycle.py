import io
import tempfile
import threading
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from manager.execution_lifecycle import enter_running_gate
from manager.executions import main as executions_main, reserve_execution, start_execution
from manager.gcs_lock_registry import RegistryConflict
from manager.tasks import TaskError, create_project, create_task, validate
from manager.test_worktree_locks import HEAD, REPO, InterleavingRegistry, MemoryRegistry, fake_git, no_preflight
from manager.worktree_locks import acquire, validate_local_preflight


def quota_document():
    return {
        "generated_at": "2026-08-13T00:00:00Z",
        "providers": [{
            "provider": "codex", "source_type": "official", "confidence": "official",
            "last_updated": "2026-08-13T00:00:00Z",
            "windows": [{"name": "primary", "used_percent": 10, "remaining_percent": 90}],
        }],
    }


class MemoryStore:
    def __init__(self):
        self.records = {}
        self.events = []
        self.mutex = threading.Lock()
        self.fail_running = False
        self.fail_task = False

    def put(self, area, project, name, document):
        with self.mutex:
            if area == "executions" and document.get("status") == "running":
                self.events.append("execution:running")
                if self.fail_running:
                    raise TaskError("running persistence failed")
            if area == "tasks" and document.get("status") == "in_progress":
                self.events.append("task:in_progress")
                if self.fail_task:
                    raise TaskError("task persistence failed")
            self.records[(area, project, name)] = deepcopy(document)
            return document

    def get(self, area, project, name):
        with self.mutex:
            return deepcopy(self.records[(area, project, name)])


def project():
    return {
        "project_id": "p1", "name": "Project", "repo": REPO, "default_branch": "main",
        "runtime_ssot": "Drive", "project_rules": [], "active_tasks": ["t1"],
        "current_phase": "Phase 3C", "important_constraints": [],
    }


def task(read_only=False, allowed_paths=None, working_directory="unused", branch="refs/heads/main", baseline_head=HEAD):
    paths = ["manager/executions.py"] if allowed_paths is None else allowed_paths
    return {
        "task_id": "t1", "project_id": "p1", "title": "Gate", "task_type": "implementation",
        "complexity": "medium", "expected_minutes": 20, "needs_repo_edit": not read_only,
        "needs_research": False, "needs_browser": False, "parallelizable": False,
        "read_only": read_only, "scope": ["manager/executions.py"], "constraints": [],
        "acceptance_criteria": ["gate"], "working_directory": working_directory,
        "branch": branch, "baseline_head": baseline_head, "allowed_paths": paths,
        "execution_policies": ["fail closed"],
    }


def build_store(read_only=False, allowed_paths=None, execution_id="exec-a", working_directory="unused", branch="refs/heads/main", baseline_head=HEAD):
    store = MemoryStore()
    create_project(store, project())
    create_task(store, task(read_only, allowed_paths, working_directory, branch, baseline_head), assign=False)
    reserve_execution(store, "p1", "t1", execution_id, "codex", {"decision": "fresh"}, "code", "high", "2026-08-13T00:00:00Z")
    store.events.clear()
    return store


class ExecutionLifecycleTests(unittest.TestCase):
    def gate(self, store, registry, execution_id="exec-a", access="production_write", **kwargs):
        options = {"baseline_head": HEAD, "started_at": "2026-08-13T00:01:00Z", "preflight_func": no_preflight}
        options.update(kwargs)
        return enter_running_gate(store, object(), registry, "p1", "t1", execution_id, "codex", access, **options)

    def assert_reserved_ready(self, store, execution_id="exec-a"):
        self.assertEqual("reserved", store.get("executions", "p1", execution_id)["status"])
        self.assertEqual("ready", store.get("tasks", "p1", "t1")["status"])

    def test_production_happy_path_orders_preflight_acquire_quota_execution_task(self):
        store = build_store(); registry = MemoryRegistry()

        def preflight(*_args):
            store.events.append("preflight")

        def ordered_acquire(*args, **kwargs):
            store.events.append("acquire")
            return acquire(*args, **kwargs)

        def read_quota(**_kwargs):
            store.events.append("quota")
            return quota_document()

        with patch("manager.execution_lifecycle.read_drive_status", side_effect=read_quota):
            result = self.gate(store, registry, preflight_func=preflight, acquire_func=ordered_acquire)
        self.assertEqual(["preflight", "acquire", "preflight", "quota", "execution:running", "task:in_progress"], store.events)
        running = result["execution"]
        self.assertEqual("running", running["status"])
        self.assertEqual("2026-08-13T00:01:00Z", running["started_at"])
        self.assertEqual("known", running["quota_before"]["status"])
        self.assertEqual("official", running["source_confidence"])
        self.assertEqual("production_write", running["access"])
        self.assertEqual("acquired", running["lease_evidence"]["authority"])
        self.assertNotIn("lease_token", running["lease_evidence"])
        self.assertIn("lease_token", result["lease"])
        self.assertNotIn(result["lease"]["lease_token"], repr(store.records))
        invalid = deepcopy(running); invalid["lease_evidence"] = None
        with self.assertRaises(TaskError):
            validate("execution", invalid)
        self.assertEqual("in_progress", store.get("tasks", "p1", "t1")["status"])
        self.assertEqual("active", result["lease"]["effective_status"])

    def test_two_execution_ids_compete_for_one_writer(self):
        store = build_store()
        reserve_execution(store, "p1", "t1", "exec-b", "codex", {"decision": "fresh"}, "code", "high", "2026-08-13T00:00:01Z")
        registry = InterleavingRegistry("exec-a")
        results, errors = [], []

        def run(execution_id):
            try:
                results.append(self.gate(store, registry, execution_id)["execution"]["execution_id"])
            except TaskError as exc:
                errors.append(str(exc))

        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            threads = [threading.Thread(target=run, args=(value,)) for value in ("exec-a", "exec-b")]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=3)
        self.assertEqual(["exec-a"], results)
        self.assertEqual(1, len(errors))
        self.assertEqual("running", store.get("executions", "p1", "exec-a")["status"])
        self.assertEqual("reserved", store.get("executions", "p1", "exec-b")["status"])
        self.assertEqual(1, sum(lock["status"] == "active" for lock in registry.document["locks"].values()))

    def test_identity_snapshot_and_access_conflicts_fail_before_preflight(self):
        for changes in ({"task_id": "other"}, {"provider": "claude"}):
            store = build_store(); preflight = Mock(); acquire_mock = Mock()
            arguments = {"task_id": "t1", "provider": "codex", **changes}
            with self.subTest(changes=changes), self.assertRaises(TaskError):
                enter_running_gate(store, object(), MemoryRegistry(), "p1", arguments["task_id"], "exec-a", arguments["provider"], "production_write", baseline_head=HEAD, preflight_func=preflight, acquire_func=acquire_mock)
            preflight.assert_not_called(); acquire_mock.assert_not_called(); self.assert_reserved_ready(store)

        store = build_store(); changed = store.get("tasks", "p1", "t1"); changed["scope"] = ["changed.py"]
        store.put("tasks", "p1", "t1", changed)
        preflight = Mock(); acquire_mock = Mock()
        with self.assertRaisesRegex(TaskError, "snapshot"):
            self.gate(store, MemoryRegistry(), preflight_func=preflight, acquire_func=acquire_mock)
        preflight.assert_not_called(); acquire_mock.assert_not_called(); self.assert_reserved_ready(store)

    def test_preflight_failures_and_unsafe_scope_never_call_acquire_or_launch(self):
        with tempfile.TemporaryDirectory() as clone:
            runners = {
                "wrong origin/repository": fake_git(remote="https://github.com/other/repo.git"),
                "wrong full branch": fake_git(branch="refs/heads/other"),
                "detached HEAD": fake_git(detached=True),
                "baseline changed": fake_git(head="b" * 40),
            }
            for reason, runner in runners.items():
                store = build_store(working_directory=clone); acquire_mock = Mock(); launch = Mock()
                preflight = lambda *args, runner=runner: validate_local_preflight(*args, runner=runner)
                def attempt():
                    self.gate(store, MemoryRegistry(), preflight_func=preflight, acquire_func=acquire_mock)
                    launch()
                with self.subTest(reason=reason), self.assertRaises(TaskError):
                    attempt()
                acquire_mock.assert_not_called(); launch.assert_not_called(); self.assert_reserved_ready(store)

        for reason, store, preflight in (
            ("unavailable working directory", build_store(working_directory="missing"), validate_local_preflight),
            ("unavailable local git", build_store(), Mock(side_effect=TaskError("local Git preflight failed"))),
            ("non-full branch", build_store(branch="main"), Mock()),
        ):
            acquire_mock = Mock()
            with self.subTest(reason=reason), self.assertRaises(TaskError):
                self.gate(store, MemoryRegistry(), preflight_func=preflight, acquire_func=acquire_mock)
            acquire_mock.assert_not_called(); self.assert_reserved_ready(store)

        store = build_store(allowed_paths=["../unsafe"]); preflight = Mock(); acquire_mock = Mock()
        with self.assertRaisesRegex(TaskError, "scope"):
            self.gate(store, MemoryRegistry(), preflight_func=preflight, acquire_func=acquire_mock)
        preflight.assert_not_called(); acquire_mock.assert_not_called(); self.assert_reserved_ready(store)

        store = build_store(allowed_paths=[]); preflight = Mock(); acquire_mock = Mock()
        with self.assertRaisesRegex(TaskError, "scope"):
            self.gate(store, MemoryRegistry(), preflight_func=preflight, acquire_func=acquire_mock)
        preflight.assert_not_called(); acquire_mock.assert_not_called(); self.assert_reserved_ready(store)

        store = build_store(); project_record = store.get("projects", "p1", "p1"); project_record["repo"] = "not-a-github-repository"
        store.put("projects", "p1", "p1", project_record); acquire_mock = Mock()
        with self.assertRaisesRegex(TaskError, "repository"):
            self.gate(store, MemoryRegistry(), acquire_func=acquire_mock)
        acquire_mock.assert_not_called(); self.assert_reserved_ready(store)

        store = build_store(baseline_head=None); acquire_mock = Mock()
        with self.assertRaisesRegex(TaskError, "baseline_head"):
            self.gate(store, MemoryRegistry(), baseline_head=None, acquire_func=acquire_mock)
        acquire_mock.assert_not_called(); self.assert_reserved_ready(store)

        store = build_store(); acquire_mock = Mock()
        with self.assertRaisesRegex(TaskError, "does not match the reservation"):
            self.gate(store, MemoryRegistry(), baseline_head="b" * 40, acquire_func=acquire_mock)
        acquire_mock.assert_not_called(); self.assert_reserved_ready(store)

    def test_acquire_contention_registry_and_cas_failures_leave_reserved(self):
        class Unavailable:
            def read(self): raise OSError("registry unavailable")

        class NeverWins(MemoryRegistry):
            def cas(self, _version, _document): raise RegistryConflict("contention")

        contention = MemoryRegistry()
        acquire(contention, "p1", "other-task", "other-exec", "claude", repository=REPO, branch="refs/heads/main", scope=["other.py"], baseline_head=HEAD, working_directory="unused", preflight_func=no_preflight)
        malformed = MemoryRegistry({"schema_version": "0.2.0", "locks": {"bad": {}}})
        for name, registry in (("contention", contention), ("missing", None), ("unavailable", Unavailable()), ("malformed", malformed), ("cas", NeverWins())):
            store = build_store(); launch = Mock()
            def attempt():
                self.gate(store, registry)
                launch()
            with self.subTest(name=name), self.assertRaises((TaskError, OSError)):
                attempt()
            launch.assert_not_called(); self.assert_reserved_ready(store)
        self.assertEqual("active", next(iter(contention.document["locks"].values()))["status"])

    def test_advisory_check_result_cannot_authorize_running(self):
        store = build_store(); advisory = Mock(return_value={"authority": "advisory_only", "safe": True})
        with self.assertRaisesRegex(TaskError, "authoritative writer acquire"):
            self.gate(store, MemoryRegistry(), acquire_func=advisory)
        advisory.assert_called_once(); self.assert_reserved_ready(store)
        with self.assertRaisesRegex(TaskError, "legacy start is retired"):
            start_execution(store, object(), "p1", "t1", "exec-a", "codex")
        self.assert_reserved_ready(store)

        wrong_owner = Mock(return_value={"authority": "acquired", "effective_status": "active", "lease_token": "x" * 32, "project_id": "p1", "task_id": "t1", "execution_id": "other", "provider": "codex"})
        with self.assertRaisesRegex(TaskError, "authoritative writer acquire"):
            self.gate(store, MemoryRegistry(), acquire_func=wrong_owner)
        self.assert_reserved_ready(store)

    def test_read_only_bypasses_all_lease_calls_and_cannot_upgrade(self):
        store = build_store(read_only=True); acquire_mock = Mock(); release_mock = Mock(); preflight = Mock()
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            result = self.gate(store, None, access="read_only", acquire_func=acquire_mock, release_func=release_mock, preflight_func=preflight)
        acquire_mock.assert_not_called(); release_mock.assert_not_called(); preflight.assert_not_called()
        self.assertEqual("read_only", result["execution"]["access"])
        self.assertIsNone(result["execution"]["lease_evidence"])
        self.assertIsNone(result["lease"])

        acquire_mock.reset_mock()
        with self.assertRaisesRegex(TaskError, "reserved execution"):
            self.gate(store, MemoryRegistry(), access="production_write", acquire_func=acquire_mock)
        acquire_mock.assert_not_called()
        fresh = build_store(read_only=True, execution_id="read-only-reserved")
        with self.assertRaisesRegex(TaskError, "cannot upgrade"):
            self.gate(fresh, MemoryRegistry(), execution_id="read-only-reserved", access="production_write", acquire_func=acquire_mock)
        acquire_mock.assert_not_called(); self.assert_reserved_ready(fresh, "read-only-reserved")

        inconsistent = build_store(read_only=True)
        task_record = inconsistent.get("tasks", "p1", "t1"); task_record["needs_repo_edit"] = True
        execution_record = inconsistent.get("executions", "p1", "exec-a"); execution_record["task_snapshot"]["needs_repo_edit"] = True
        inconsistent.put("tasks", "p1", "t1", task_record); inconsistent.put("executions", "p1", "exec-a", execution_record)
        with self.assertRaisesRegex(TaskError, "explicitly read-only"):
            self.gate(inconsistent, None, access="read_only", acquire_func=acquire_mock)
        acquire_mock.assert_not_called(); self.assert_reserved_ready(inconsistent)

    def test_crash_windows_do_not_prewrite_running_state(self):
        store = build_store()
        with self.assertRaises(SystemExit):
            self.gate(store, MemoryRegistry(), acquire_func=Mock(side_effect=SystemExit("crash before acquire")))
        self.assert_reserved_ready(store)

        store = build_store(); registry = MemoryRegistry()
        with patch("manager.execution_lifecycle.read_drive_status", side_effect=SystemExit("crash after acquire")):
            with self.assertRaises(SystemExit):
                self.gate(store, registry)
        self.assert_reserved_ready(store)
        self.assertEqual("active", next(iter(registry.document["locks"].values()))["status"])

    def test_running_or_task_persistence_failure_rolls_back_and_releases(self):
        for failure in ("running", "task"):
            store = build_store(); registry = MemoryRegistry(); launch = Mock()
            setattr(store, f"fail_{failure}", True)
            def attempt():
                self.gate(store, registry)
                launch()
            with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
                with self.subTest(failure=failure), self.assertRaisesRegex(TaskError, "persistence failed"):
                    attempt()
            launch.assert_not_called(); self.assert_reserved_ready(store)
            self.assertEqual("released", next(iter(registry.document["locks"].values()))["status"])

    def test_cleanup_failure_is_observable(self):
        store = build_store(); store.fail_running = True
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            with self.assertRaisesRegex(TaskError, "lease release failed"):
                self.gate(store, MemoryRegistry(), release_func=Mock(side_effect=OSError("cleanup unavailable")))
        self.assert_reserved_ready(store)

    def test_legacy_api_and_cli_cannot_create_or_bypass_reservation(self):
        store = build_store(); task_before = store.get("tasks", "p1", "t1")
        for execution_id in ("missing", "exec-b", "exec-a"):
            with self.subTest(execution_id=execution_id), self.assertRaisesRegex(TaskError, "legacy start is retired"):
                start_execution(store, object(), "p1", "t1", execution_id, "codex")
        self.assertNotIn(("executions", "p1", "exec-b"), store.records)
        self.assertEqual(task_before, store.get("tasks", "p1", "t1"))

        args = ["executions.py", "start", "p1", "t1", "exec-b", "--provider", "codex"]
        with patch("manager.executions.build_service", return_value=object()), patch("manager.executions.DriveRecords", return_value=store), patch("sys.argv", args), patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(1, executions_main())
        self.assertIn("legacy start is retired", stderr.getvalue())
        self.assertNotIn(("executions", "p1", "exec-b"), store.records)
        self.assertEqual(task_before, store.get("tasks", "p1", "t1"))


if __name__ == "__main__":
    unittest.main()
