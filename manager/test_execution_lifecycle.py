import io
import inspect
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from manager.execution_lifecycle import TaskClaimConflict, _cas_task, _read_task_version, enter_running_gate
from manager.executions import main as executions_main, reserve_execution, start_execution
from manager.gcs_lock_registry import RegistryConflict
from manager.tasks import TaskError, create_project, create_task, validate
from manager.test_worktree_locks import HEAD, REPO, InterleavingRegistry, MemoryRegistry, fake_git, no_preflight
from manager.worktree_locks import acquire, release, validate_local_preflight


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
        self.versions = {}
        self.fail_running = False
        self.fail_task = False
        self.fail_execution_verification = False
        self.fail_execution_rollback = False
        self.fail_task_rollback = False
        self.claim_read_barrier = None

    def put(self, area, project, name, document):
        with self.mutex:
            current = self.records.get((area, project, name))
            if area == "executions" and document.get("status") == "running":
                self.events.append("execution:running")
                if self.fail_running:
                    raise TaskError("running persistence failed")
            if area == "executions" and document.get("status") == "reserved" and current and current.get("status") == "running" and self.fail_execution_rollback:
                raise TaskError("execution rollback failed")
            if area == "tasks" and document.get("status") == "in_progress":
                self.events.append("task:in_progress")
                if self.fail_task:
                    raise TaskError("task persistence failed")
            self.records[(area, project, name)] = deepcopy(document)
            self.versions[(area, project, name)] = self.versions.get((area, project, name), 0) + 1
            return document

    def get(self, area, project, name):
        with self.mutex:
            if area == "executions" and self.fail_execution_verification and self.records[(area, project, name)].get("status") == "running":
                self.fail_execution_verification = False
                raise TaskError("running persistence verification failed")
            return deepcopy(self.records[(area, project, name)])

    def read_task_for_claim(self, project, name):
        with self.mutex:
            key = ("tasks", project, name)
            result = deepcopy(self.records[key]), self.versions[key]
        barrier = self.claim_read_barrier
        if barrier:
            barrier.wait(timeout=3)
            self.claim_read_barrier = None
        return result

    def cas_task_claim(self, project, name, version, document):
        with self.mutex:
            key = ("tasks", project, name)
            if self.versions[key] != version:
                raise TaskClaimConflict("task active execution claim changed concurrently")
            current = self.records[key]
            if document.get("status") == "in_progress":
                self.events.append("task:in_progress")
                if self.fail_task:
                    raise TaskError("task persistence failed")
            if document.get("status") == "ready" and current.get("status") == "in_progress" and self.fail_task_rollback:
                raise TaskError("task claim rollback failed")
            self.records[key] = deepcopy(document)
            self.versions[key] += 1
            return deepcopy(document), self.versions[key]


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
        preflight_func = kwargs.pop("preflight_func", no_preflight)
        acquire_func = kwargs.pop("acquire_func", acquire)
        release_func = kwargs.pop("release_func", release)
        options = {"baseline_head": HEAD, "started_at": "2026-08-13T00:01:00Z"}
        options.update(kwargs)
        with patch("manager.execution_lifecycle.validate_local_preflight", side_effect=preflight_func), patch("manager.execution_lifecycle.acquire", side_effect=acquire_func), patch("manager.execution_lifecycle.release", side_effect=release_func):
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
        self.assertEqual(["preflight", "acquire", "preflight", "quota", "task:in_progress", "execution:running"], store.events)
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
            with patch("manager.execution_lifecycle.validate_local_preflight", preflight), patch("manager.execution_lifecycle.acquire", acquire_mock):
                with self.subTest(changes=changes), self.assertRaises(TaskError):
                    enter_running_gate(store, object(), MemoryRegistry(), "p1", arguments["task_id"], "exec-a", arguments["provider"], "production_write", baseline_head=HEAD)
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
        with self.assertRaisesRegex(TaskError, "acquire validation failed"):
            self.gate(store, MemoryRegistry(), acquire_func=wrong_owner)
        self.assert_reserved_ready(store)

    def test_no_callable_accepts_serialized_lease_evidence_as_running_authority(self):
        self.assertNotIn("acquire_func", inspect.signature(enter_running_gate).parameters)
        self.assertNotIn("lease_evidence", inspect.signature(enter_running_gate).parameters)
        self.assertFalse(hasattr(__import__("manager.executions", fromlist=["_mark_execution_running"]), "_mark_execution_running"))
        store = build_store()
        fake = {
            "authority": "acquired", "lock_id": "repo-" + "0" * 64, "generation": 1,
            "repository": "github:ne9221/ai-development-manager", "branch": "refs/heads/main",
            "scope": ["manager/executions.py"], "baseline_head": HEAD,
        }
        with self.assertRaises(TypeError):
            enter_running_gate(store, object(), MemoryRegistry(), "p1", "t1", "exec-a", "codex", "production_write", lease_evidence=fake)
        self.assert_reserved_ready(store)

    def test_two_read_only_reservations_can_only_claim_task_once(self):
        store = build_store(read_only=True)
        reserve_execution(store, "p1", "t1", "exec-b", "codex", {"decision": "fresh"}, "code", "high", "2026-08-13T00:00:01Z")
        store.claim_read_barrier = threading.Barrier(2)
        results, errors = [], []

        def run(execution_id):
            try:
                result = enter_running_gate(store, object(), None, "p1", "t1", execution_id, "codex", "read_only", started_at="2026-08-13T00:01:00Z")
                results.append(result["execution"]["execution_id"])
            except TaskError as exc:
                errors.append(str(exc))

        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            threads = [threading.Thread(target=run, args=(value,)) for value in ("exec-a", "exec-b")]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=3)
        self.assertEqual(1, len(results)); self.assertEqual(1, len(errors))
        winner = results[0]; loser = "exec-b" if winner == "exec-a" else "exec-a"
        self.assertEqual("running", store.get("executions", "p1", winner)["status"])
        self.assertEqual("reserved", store.get("executions", "p1", loser)["status"])
        claimed = store.get("tasks", "p1", "t1")
        self.assertEqual("in_progress", claimed["status"])
        self.assertEqual(winner, claimed["source_context"]["active_execution_id"])

    def test_drive_etag_cas_rejects_a_stale_task_claim(self):
        document = build_store(read_only=True).get("tasks", "p1", "t1")

        class PreconditionFailed(Exception):
            def __init__(self):
                self.resp = type("Response", (), {"status": 412})()

        class Request:
            def __init__(self, action):
                self.action = action
                self.headers = {}
                self.postproc = lambda _response, content: content

            def execute(self):
                return self.action(self)

        class Files:
            def __init__(self):
                self.document = deepcopy(document)
                self.etag = '"1"'
                self.seen_if_match = []

            def get_media(self, fileId):
                self.assert_file(fileId)
                return Request(lambda request: request.postproc({"etag": self.etag}, (json.dumps(self.document) + "\n").encode()))

            def update(self, fileId, body, media_body, **_kwargs):
                self.assert_file(fileId)

                def update(request):
                    self.seen_if_match.append(request.headers.get("If-Match"))
                    if request.headers.get("If-Match") != self.etag:
                        raise PreconditionFailed()
                    self.document = json.loads(media_body.getbytes(0, media_body.size()))
                    self.etag = '"2"'
                    return {"id": fileId}

                return Request(update)

            @staticmethod
            def assert_file(file_id):
                if file_id != "task-file":
                    raise AssertionError(file_id)

        class Store:
            def __init__(self): self.files = Files()
            def _record_match(self, _area, _project, _name):
                return "parent", "t1.json", {"id": "task-file"}

        store = Store()
        first, first_version = _read_task_version(store, "p1", "t1")
        _, stale_version = _read_task_version(store, "p1", "t1")
        first["source_context"]["active_execution_id"] = "exec-a"
        _cas_task(store, "p1", "t1", first_version, first)
        stale = deepcopy(document); stale["source_context"]["active_execution_id"] = "exec-b"
        with self.assertRaisesRegex(TaskClaimConflict, "changed concurrently"):
            _cas_task(store, "p1", "t1", stale_version, stale)
        self.assertEqual(['"1"', '"1"'], store.files.seen_if_match)
        self.assertEqual("exec-a", store.files.document["source_context"]["active_execution_id"])

    def test_production_cannot_overwrite_an_existing_task_claim(self):
        store = build_store(); registry = MemoryRegistry()
        claimed = store.get("tasks", "p1", "t1")
        claimed["source_context"]["active_execution_id"] = "other-execution"
        store.put("tasks", "p1", "t1", claimed)
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            with self.assertRaisesRegex(TaskError, "already claimed"):
                self.gate(store, registry)
        self.assert_reserved_ready(store)
        self.assertEqual("other-execution", store.get("tasks", "p1", "t1")["source_context"]["active_execution_id"])
        self.assertEqual("released", next(iter(registry.document["locks"].values()))["status"])

    def test_acquire_validation_failure_releases_real_lease(self):
        store = build_store(); registry = MemoryRegistry()

        def mismatching_acquire(*args, **kwargs):
            result = acquire(*args, **kwargs)
            result["execution_id"] = "wrong-owner"
            return result

        with self.assertRaisesRegex(TaskError, "authoritative writer acquire"):
            self.gate(store, registry, acquire_func=mismatching_acquire)
        self.assert_reserved_ready(store)
        self.assertEqual("released", next(iter(registry.document["locks"].values()))["status"])

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
            if failure == "task":
                self.assertNotIn("execution:running", store.events)

    def test_running_verification_failure_rolls_back_before_release(self):
        store = build_store(); registry = MemoryRegistry(); store.fail_execution_verification = True
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            with self.assertRaisesRegex(TaskError, "verification failed"):
                self.gate(store, registry)
        self.assert_reserved_ready(store)
        self.assertEqual("released", next(iter(registry.document["locks"].values()))["status"])

    def test_unconfirmed_execution_rollback_retains_task_claim_and_lease(self):
        store = build_store(); registry = MemoryRegistry(); launch = Mock()
        store.fail_execution_verification = True
        store.fail_execution_rollback = True

        def attempt():
            self.gate(store, registry)
            launch()

        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            with self.assertRaisesRegex(TaskError, "recovery required; lease retained"):
                attempt()
        launch.assert_not_called()
        self.assertEqual("running", store.get("executions", "p1", "exec-a")["status"])
        claimed = store.get("tasks", "p1", "t1")
        self.assertEqual("in_progress", claimed["status"])
        self.assertEqual("exec-a", claimed["source_context"]["active_execution_id"])
        self.assertEqual("active", next(iter(registry.document["locks"].values()))["status"])

    def test_unconfirmed_task_rollback_retains_claim_and_lease(self):
        store = build_store(); registry = MemoryRegistry(); launch = Mock()
        store.fail_running = True
        store.fail_task_rollback = True

        def attempt():
            self.gate(store, registry)
            launch()

        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            with self.assertRaisesRegex(TaskError, "recovery required; lease retained"):
                attempt()
        launch.assert_not_called()
        self.assertEqual("reserved", store.get("executions", "p1", "exec-a")["status"])
        claimed = store.get("tasks", "p1", "t1")
        self.assertEqual("in_progress", claimed["status"])
        self.assertEqual("exec-a", claimed["source_context"]["active_execution_id"])
        self.assertEqual("active", next(iter(registry.document["locks"].values()))["status"])

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
