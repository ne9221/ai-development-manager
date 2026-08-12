import subprocess
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from manager.gcs_lock_registry import RegistryConflict
from manager.tasks import TaskError
from manager.worktree_locks import acquire, canonical_branch, canonical_repository, canonical_scope, check, inspect, list_locks, release, renew, repository_lock_id, semantic_lock, validate_local_preflight


NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)
HEAD = "a" * 40
REPO = "https://github.com/Example/Repo.git"


class MemoryRegistry:
    def __init__(self, document=None):
        self.document = deepcopy(document or {"schema_version": "0.2.0", "locks": {}})
        self.version = 1
        self.now = NOW
        self.mutex = threading.Lock()

    def read(self):
        with self.mutex:
            return deepcopy(self.document), str(self.version), self.now

    def cas(self, version, document):
        with self.mutex:
            if version != str(self.version):
                raise RegistryConflict("stale")
            self.document = deepcopy(document)
            self.version += 1


class InterleavingRegistry(MemoryRegistry):
    def __init__(self, preferred_execution):
        super().__init__()
        self.preferred_execution = preferred_execution
        self.barrier = threading.Barrier(2)
        self.preferred_done = threading.Event()
        self.initial_reads = 0

    def read(self):
        snapshot = super().read()
        with self.mutex:
            first_round = self.initial_reads < 2
            self.initial_reads += 1
        if first_round:
            self.barrier.wait(timeout=2)
        return snapshot

    def cas(self, version, document):
        execution = next(iter(document["locks"].values()))["execution_id"]
        if execution != self.preferred_execution:
            self.preferred_done.wait(timeout=2)
        try:
            return super().cas(version, document)
        finally:
            if execution == self.preferred_execution:
                self.preferred_done.set()


def no_preflight(*_args):
    return {}


def acquire_args(execution_id="exec-a", **changes):
    value = {
        "project_id": "p1", "task_id": "task-a", "execution_id": execution_id,
        "provider": "codex", "session_id": "codex:session-a", "repository": REPO,
        "branch": "main", "scope": ["manager/a.py"], "baseline_head": HEAD,
        "working_directory": "unused", "preflight_func": no_preflight,
    }
    value.update(changes)
    return value


def owner_from(result, **changes):
    value = {key: result[key] for key in ("project_id", "task_id", "execution_id", "provider", "session_id")}
    value.update(changes)
    return value


def fake_git(remote=REPO, branch="refs/heads/main", head=HEAD, detached=False):
    def runner(command, **_kwargs):
        args = command[3:]
        if args[:3] == ["remote", "get-url", "--all"]:
            return subprocess.CompletedProcess(command, 0, remote + "\n", "")
        if args[:3] == ["symbolic-ref", "--quiet", "HEAD"]:
            return subprocess.CompletedProcess(command, 1 if detached else 0, "" if detached else branch + "\n", "")
        if args[:2] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, head + "\n", "")
        return subprocess.CompletedProcess(command, 1, "", "unexpected")
    return runner


class WorktreeLockTests(unittest.TestCase):
    def test_atomic_interleavings_have_exactly_one_winner(self):
        for preferred in ("exec-a", "exec-b"):
            registry = InterleavingRegistry(preferred)
            results, errors = [], []

            def run(execution):
                try:
                    results.append(acquire(registry, **acquire_args(execution, task_id=f"task-{execution}", session_id=f"codex:{execution}")))
                except TaskError as exc:
                    errors.append(str(exc))

            threads = [threading.Thread(target=run, args=(execution,)) for execution in ("exec-a", "exec-b")]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=3)
            self.assertEqual([preferred], [item["execution_id"] for item in results])
            self.assertEqual(1, len(errors))

    def test_coarse_repo_lock_blocks_branches_and_disjoint_scope(self):
        registry = MemoryRegistry(); acquire(registry, **acquire_args())
        with self.assertRaisesRegex(TaskError, "active production writer"):
            acquire(registry, **acquire_args("exec-b", task_id="task-b", session_id="codex:b", branch="feature/b", scope=["docs/b.md"]))

    def test_same_owner_retry_requires_and_accepts_token(self):
        registry = MemoryRegistry(); first = acquire(registry, **acquire_args())
        with self.assertRaisesRegex(TaskError, "active production writer"):
            acquire(registry, **acquire_args())
        second = acquire(registry, **acquire_args(lease_token=first["lease_token"]))
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual(first["lease_token"], second["lease_token"])

    def test_wrong_owner_cannot_release_or_renew(self):
        registry = MemoryRegistry(); result = acquire(registry, **acquire_args())
        for change in ({"execution_id": "other"}, {"project_id": "other"}, {"provider": "claude"}, {"session_id": "claude:other"}):
            with self.assertRaisesRegex(TaskError, "owner verification"):
                release(registry, result["lock_id"], **owner_from(result, **change), lease_token=result["lease_token"])
            with self.assertRaisesRegex(TaskError, "owner verification"):
                renew(registry, result["lock_id"], **owner_from(result, **change), lease_token=result["lease_token"])

    def test_correct_release_and_double_release(self):
        registry = MemoryRegistry(); result = acquire(registry, **acquire_args())
        first = release(registry, result["lock_id"], **owner_from(result), lease_token=result["lease_token"])
        self.assertEqual(3, registry.version)
        second = release(registry, result["lock_id"], **owner_from(result), lease_token=result["lease_token"])
        self.assertEqual(3, registry.version)
        self.assertEqual("released", first["status"]); self.assertEqual(first, second)
        self.assertNotIn("lease_token", first); self.assertNotIn("lease_token_hash", first)

    def test_renew_owner_expiry_and_new_generation(self):
        registry = MemoryRegistry(); first = acquire(registry, **acquire_args(ttl_minutes=10))
        registry.now += timedelta(minutes=5)
        renewed = renew(registry, first["lock_id"], **owner_from(first), lease_token=first["lease_token"], ttl_minutes=20)
        self.assertEqual(3, registry.version)
        self.assertEqual("active", renewed["effective_status"])
        registry.now += timedelta(minutes=21)
        with self.assertRaisesRegex(TaskError, "expired lease cannot be renewed"):
            renew(registry, first["lock_id"], **owner_from(first), lease_token=first["lease_token"])
        second = acquire(registry, **acquire_args("exec-b", task_id="task-b", session_id="codex:b"))
        self.assertEqual(first["generation"] + 1, second["generation"])
        with self.assertRaisesRegex(TaskError, "owner verification"):
            renew(registry, first["lock_id"], **owner_from(first), lease_token=first["lease_token"])
        with self.assertRaisesRegex(TaskError, "owner verification"):
            release(registry, first["lock_id"], **owner_from(first), lease_token=first["lease_token"])

    def test_repository_normalization_credentials_clones_and_fork(self):
        expected = "github:owner/repo"
        for value in ("https://github.com/Owner/Repo", "https://github.com/owner/repo.git", "git@github.com:owner/repo.git", "ssh://git@github.com/owner/repo"):
            self.assertEqual(expected, canonical_repository(value))
        self.assertNotEqual(repository_lock_id(expected), repository_lock_id(canonical_repository("https://github.com/fork/repo")))
        for value in ("C:/clone/repo", "https://user:token@github.com/owner/repo", "https://example.com/owner/repo"):
            with self.assertRaises(TaskError): canonical_repository(value)
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            for clone in (first, second):
                self.assertEqual(expected, validate_local_preflight(clone, expected, "refs/heads/main", HEAD, fake_git(remote="https://github.com/owner/repo.git"))["repository"])

    def test_branch_contract_preserves_case_and_rejects_detached(self):
        self.assertEqual("refs/heads/main", canonical_branch("main"))
        self.assertEqual("refs/heads/Feature/X", canonical_branch("refs/heads/Feature/X"))
        self.assertNotEqual(canonical_branch("Feature/X"), canonical_branch("feature/x"))
        for value in ("HEAD", HEAD, "refs/tags/v1", "main..bad"):
            with self.assertRaises(TaskError): canonical_branch(value)
        with tempfile.TemporaryDirectory() as clone:
            with self.assertRaisesRegex(TaskError, "symbolic-ref"):
                validate_local_preflight(clone, "github:example/repo", "refs/heads/main", HEAD, fake_git(detached=True))

    def test_scope_contract_root_slashes_and_rejections(self):
        self.assertEqual(["."], canonical_scope([".", "manager/a.py"]))
        self.assertEqual(["manager/a.py"], canonical_scope(["manager\\a.py", "manager/a.py/"]))
        for value in ([".."], ["src/../x"], ["src/./x"], ["*.py"], ["C:\\repo\\x"], ["/repo/x"], ["src//x"]):
            with self.assertRaises(TaskError): canonical_scope(value)

    def test_local_preflight_mismatch_and_valid(self):
        with tempfile.TemporaryDirectory() as clone:
            self.assertEqual(HEAD, validate_local_preflight(clone, "github:example/repo", "refs/heads/main", HEAD, fake_git())["baseline_head"])
            with self.assertRaisesRegex(TaskError, "origin"):
                validate_local_preflight(clone, "github:other/repo", "refs/heads/main", HEAD, fake_git())
            with self.assertRaisesRegex(TaskError, "branch"):
                validate_local_preflight(clone, "github:example/repo", "refs/heads/other", HEAD, fake_git())
            with self.assertRaisesRegex(TaskError, "HEAD"):
                validate_local_preflight(clone, "github:example/repo", "refs/heads/main", "b" * 40, fake_git())
        with self.assertRaisesRegex(TaskError, "working directory"):
            validate_local_preflight("missing", "github:example/repo", "refs/heads/main", HEAD, fake_git())

    def test_check_is_advisory_validated_and_read_only_cannot_upgrade(self):
        registry = MemoryRegistry()
        request = acquire_args(); request.pop("working_directory"); request.pop("preflight_func"); request["access"] = "production"
        preview = check(registry, request, "unused", no_preflight)
        self.assertTrue(preview["safe"]); self.assertEqual("advisory_only", preview["authority"])
        malformed = {**request, "access": "invalid", "repository": None}
        with self.assertRaises(TaskError): check(registry, malformed, "unused", no_preflight)
        readonly = {key: request[key] for key in ("project_id", "task_id", "execution_id", "provider", "session_id")}; readonly["access"] = "read_only"
        self.assertTrue(check(registry, readonly)["safe"])
        with self.assertRaisesRegex(TaskError, "cannot acquire or upgrade"):
            acquire(registry, **acquire_args(access="read_only"))

    def test_backend_unavailable_malformed_registry_and_redaction_fail_closed(self):
        class Unavailable:
            def read(self): raise OSError("registry unavailable")
        with self.assertRaises(OSError): acquire(Unavailable(), **acquire_args())
        malformed = MemoryRegistry({"schema_version": "0.2.0", "locks": {"bad": {}}})
        with self.assertRaises(TaskError): check(malformed, {**{k: v for k, v in acquire_args().items() if k not in ("working_directory", "preflight_func")}, "access": "production"}, "unused", no_preflight)
        registry = MemoryRegistry(); result = acquire(registry, **acquire_args())
        viewed = inspect(registry, result["lock_id"])
        self.assertNotIn("lease_token", viewed); self.assertNotIn("lease_token_hash", viewed)
        self.assertNotIn("lease_token_hash", list_locks(registry)[0])

    def test_schema_semantic_timestamp_invariants(self):
        registry = MemoryRegistry(); result = acquire(registry, **acquire_args())
        raw = next(iter(registry.document["locks"].values()))
        broken = {**raw, "expires_at": raw["created_at"]}
        with self.assertRaisesRegex(TaskError, "timestamp ordering"):
            semantic_lock(broken, raw["lock_id"])
        broken = {**raw, "status": "released", "released_at": None}
        with self.assertRaises(TaskError): semantic_lock(broken, raw["lock_id"])


if __name__ == "__main__": unittest.main()
