import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from manager.claude_config_locks import (
    ConfigLockBusyError,
    acquire_claude_config_lock,
    canonical_config_dir,
    config_lock_id,
    release_claude_config_lock,
)
from manager.codex_launcher import process_creation_identity
from manager.tasks import TaskError


class ConfigLockTestCase(unittest.TestCase):
    """Every test uses its own isolated state/lock file pair -- never the
    real AI_MANAGER_HOME default -- so this suite can never read or write
    real ADM runtime state or a real Claude config directory."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.state_path = Path(self.home.name) / "state.json"
        self.lock_path = Path(self.home.name) / "state.lock"

    def tearDown(self):
        self.home.cleanup()

    def acquire(self, config_dir=r"C:\accounts\a\.claude", **kwargs):
        kwargs.setdefault("account_id", "account-a")
        kwargs.setdefault("execution_id", "exec-a")
        return acquire_claude_config_lock(config_dir, state_path=self.state_path, lock_path=self.lock_path, **kwargs)

    def release(self, record):
        return release_claude_config_lock(record, state_path=self.state_path, lock_path=self.lock_path)


class CanonicalizationTests(unittest.TestCase):
    def test_none_resolves_to_env_override_when_set(self):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": r"C:\envset\.claude"}):
            self.assertEqual(canonical_config_dir(r"C:\envset\.claude"), canonical_config_dir(None))

    def test_none_resolves_to_home_dot_claude_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            expected = canonical_config_dir(str(Path.home() / ".claude"))
            self.assertEqual(expected, canonical_config_dir(None))
            self.assertEqual(expected, canonical_config_dir(""))
            self.assertEqual(expected, canonical_config_dir("   "))

    def test_case_insensitive_on_windows(self):
        if os.name != "nt":
            self.skipTest("case-insensitive canonicalization is Windows-specific")
        self.assertEqual(canonical_config_dir(r"C:\Accounts\A\.claude"), canonical_config_dir(r"c:\accounts\a\.CLAUDE"))

    def test_trailing_separator_is_irrelevant(self):
        self.assertEqual(canonical_config_dir(r"C:\accounts\a\.claude"), canonical_config_dir(r"C:\accounts\a\.claude\ "[:-1]))
        self.assertEqual(canonical_config_dir(r"C:\accounts\a\.claude"), canonical_config_dir(r"C:\accounts\a\.claude\\"))

    def test_dot_and_dotdot_segments_normalize(self):
        self.assertEqual(
            canonical_config_dir(r"C:\accounts\a\.claude"),
            canonical_config_dir(r"C:\accounts\b\..\a\.\.claude"),
        )

    def test_forward_and_back_slash_normalize_the_same(self):
        self.assertEqual(canonical_config_dir(r"C:\accounts\a\.claude"), canonical_config_dir("C:/accounts/a/.claude"))

    def test_relative_path_is_resolved_against_cwd(self):
        with tempfile.TemporaryDirectory() as cwd:
            with patch("manager.claude_config_locks.Path.cwd", return_value=Path(cwd)):
                self.assertEqual(canonical_config_dir(str(Path(cwd) / "sub" / ".claude")), canonical_config_dir("sub/.claude"))

    def test_two_different_real_directories_do_not_collide(self):
        self.assertNotEqual(canonical_config_dir(r"C:\accounts\a\.claude"), canonical_config_dir(r"C:\accounts\b\.claude"))

    def test_lock_id_is_stable_and_does_not_embed_the_raw_path(self):
        canonical = canonical_config_dir(r"C:\Users\real-username\.claude")
        lock_id = config_lock_id(canonical)
        self.assertEqual(lock_id, config_lock_id(canonical))
        self.assertNotIn("real-username", lock_id)
        self.assertNotIn("\\", lock_id)
        self.assertTrue(lock_id.startswith("claude-config-"))

    def test_symlink_or_junction_resolves_to_its_real_target(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "real-config"
            target.mkdir()
            link = Path(root) / "link-config"
            try:
                if os.name == "nt":
                    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=True)
                else:
                    link.symlink_to(target, target_is_directory=True)
            except (OSError, subprocess.CalledProcessError):
                self.skipTest("could not create a symlink/junction in this environment")
            self.assertEqual(canonical_config_dir(str(target)), canonical_config_dir(str(link)))


class AcquireReleaseTests(ConfigLockTestCase):
    def test_acquire_then_release_round_trips_cleanly(self):
        record = self.acquire()
        self.assertEqual("CLAUDE_CONFIG_BUSY", ConfigLockBusyError.classification)
        self.assertEqual(os.getpid(), record["pid"])
        result = self.release(record)
        self.assertEqual({"released": True}, result)
        # Released: a fresh acquire for the same directory must succeed again.
        second = self.acquire()
        self.assertEqual(record["lock_id"], second["lock_id"])

    def test_missing_state_file_is_created_on_first_acquire(self):
        self.assertFalse(self.state_path.exists())
        self.acquire()
        self.assertTrue(self.state_path.exists())

    def test_same_owner_reacquire_is_idempotent(self):
        first = self.acquire()
        second = self.acquire()
        self.assertEqual(first, second)

    def test_different_config_dirs_never_contend(self):
        a = self.acquire(config_dir=r"C:\accounts\a\.claude")
        b = self.acquire(config_dir=r"C:\accounts\b\.claude", account_id="account-b", execution_id="exec-b")
        self.assertNotEqual(a["lock_id"], b["lock_id"])

    def test_two_accounts_mapped_to_the_same_real_directory_conflict(self):
        # architecture question 2: misconfigured account_id must not be
        # treated as isolation -- the real resource is the directory.
        self.acquire(config_dir=r"C:\shared\.claude", account_id="account-a", execution_id="exec-a",
                    pid=999901, creation_identity="fake-live-owner")
        with patch("manager.claude_config_locks.process_identity_state", return_value="live"):
            with self.assertRaises(ConfigLockBusyError):
                self.acquire(config_dir=r"C:\shared\.claude", account_id="account-b", execution_id="exec-b")

    def test_busy_error_never_falls_back_to_a_different_account(self):
        self.acquire(pid=999902, creation_identity="fake-live-owner")
        with patch("manager.claude_config_locks.process_identity_state", return_value="live"):
            with self.assertRaises(ConfigLockBusyError) as ctx:
                self.acquire()
        self.assertIn("exec-a", str(ctx.exception))

    def test_release_is_aba_safe_against_a_later_owner(self):
        first = self.acquire(execution_id="exec-a", pid=999910, creation_identity="owner-a")
        self.release(first)
        second = self.acquire(execution_id="exec-b", pid=999911, creation_identity="owner-b")
        # Stale release of the first (already-gone) owner must not touch the
        # second owner's now-active entry.
        result = self.release(first)
        self.assertEqual({"released": False, "reason": "owned_by_another_owner"}, result)
        # The second owner's lock must still be intact.
        third = self.acquire(execution_id="exec-b")
        self.assertEqual(second["lock_id"], third["lock_id"])

    def test_release_of_never_acquired_record_is_reported_not_raised(self):
        fake = {"lock_id": config_lock_id(canonical_config_dir(r"C:\nowhere\.claude")), "pid": 1, "creation_identity": "x"}
        self.assertEqual({"released": False, "reason": "not_held"}, self.release(fake))

    def test_release_of_empty_record_is_reported_not_raised(self):
        self.assertEqual({"released": False, "reason": "no_record"}, self.release(None))
        self.assertEqual({"released": False, "reason": "no_record"}, self.release({}))


class StaleRecoveryTests(ConfigLockTestCase):
    def test_stopped_owner_is_reclaimed(self):
        self.acquire(pid=999903, creation_identity="dead-owner", execution_id="exec-old")
        with patch("manager.claude_config_locks.process_identity_state", return_value="stopped"):
            record = self.acquire(execution_id="exec-new")
        self.assertEqual("exec-new", record["execution_id"])

    def test_replaced_pid_owner_is_reclaimed(self):
        # PID reuse: the recorded pid is alive again, but as a different
        # process (a different creation_identity) -- the original owner is
        # provably gone, not merely unverifiable.
        self.acquire(pid=999904, creation_identity="original-owner", execution_id="exec-old")
        with patch("manager.claude_config_locks.process_identity_state", return_value="replaced"):
            record = self.acquire(execution_id="exec-new")
        self.assertEqual("exec-new", record["execution_id"])

    def test_unknown_liveness_fails_closed_never_reclaimed(self):
        # ABA guard: an unverifiable owner must never be treated as safe to
        # steal, even though it "looks" idle.
        self.acquire(pid=999905, creation_identity="ambiguous-owner", execution_id="exec-old")
        with patch("manager.claude_config_locks.process_identity_state", return_value="unknown"):
            with self.assertRaises(ConfigLockBusyError):
                self.acquire(execution_id="exec-new")

    def test_a_genuinely_dead_process_is_actually_reclaimed_end_to_end(self):
        # No mocking of process_identity_state: a real subprocess is started
        # and killed, and its real pid/creation_identity are used, so this
        # proves the real OS-level liveness check, not just the plumbing.
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=5)
        identity = process_creation_identity(proc.pid) or "unavailable-after-exit"
        self.acquire(pid=proc.pid, creation_identity=identity, execution_id="exec-old")
        record = self.acquire(execution_id="exec-new")
        self.assertEqual("exec-new", record["execution_id"])


class MalformedStateTests(ConfigLockTestCase):
    def test_corrupt_json_state_file_fails_closed_on_acquire(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(TaskError):
            self.acquire()

    def test_wrong_schema_shape_fails_closed_on_acquire(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text('{"schema_version": "9.9.9", "locks": {}}', encoding="utf-8")
        with self.assertRaises(TaskError):
            self.acquire()

    def test_corrupt_state_file_release_reports_rather_than_raises(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text("{not json", encoding="utf-8")
        result = self.release({"lock_id": "claude-config-x", "pid": 1, "creation_identity": "y"})
        self.assertFalse(result["released"])

    def test_no_credential_shaped_keys_ever_appear_in_a_persisted_record(self):
        record = self.acquire()
        forbidden = {"token", "access_token", "refresh_token", "oauth", "password", "secret", "api_key"}
        leaked = forbidden & {str(key).lower() for key in record}
        self.assertEqual(set(), leaked)
        raw = self.state_path.read_text(encoding="utf-8")
        for word in ("token", "password", "secret", "api_key", "oauth"):
            self.assertNotIn(word, raw.lower())


class ConcurrencyRaceTests(ConfigLockTestCase):
    def test_many_threads_racing_the_same_config_dir_produce_exactly_one_winner(self):
        # Real OS threads, real file locking -- not mocked. Every thread
        # presents as a distinct owner (distinct fake pid/creation_identity)
        # so none of them collapse into the same-owner idempotent path;
        # only one may ever win a config directory none of them has released.
        winners, busy, errors = [], [], []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def attempt(i):
            try:
                barrier.wait(timeout=5)
                record = self.acquire(pid=999800 + i, creation_identity=f"racer-{i}", execution_id=f"exec-{i}")
                with lock:
                    winners.append(record["execution_id"])
            except ConfigLockBusyError:
                with lock:
                    busy.append(i)
            except Exception as exc:  # pragma: no cover - would fail the test below anyway
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        # Every racer's fake pid is fictitious (no real process behind it),
        # so an honest process_identity_state() would report "stopped" for a
        # loser's entry and this test would measure stale-reclaim, not the
        # race. Patched once here (never inside a thread -- mock.patch
        # itself is not thread-safe to enter/exit concurrently) so every
        # contender looks like a live, legitimate owner: this isolates
        # exactly what this test targets -- the acquire race itself must
        # have at most one winner even when nothing looks reclaimable.
        with patch("manager.claude_config_locks.process_identity_state", return_value="live"):
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        self.assertEqual([], errors)
        self.assertEqual(1, len(winners), f"expected exactly one winner, got {winners}")
        self.assertEqual(7, len(busy))

    def test_different_config_dirs_do_not_block_each_other_under_concurrency(self):
        # Same barrier-synchronized race, but every thread targets its own
        # distinct config_dir -- none of them should ever see BUSY; any
        # transient table-lock contention must be absorbed by the retry, not
        # surfaced as a false conflict between unrelated directories.
        results, errors = [], []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def attempt(i):
            try:
                barrier.wait(timeout=5)
                record = self.acquire(config_dir=fr"C:\accounts\dir-{i}\.claude", pid=999700 + i,
                                      creation_identity=f"racer-{i}", execution_id=f"exec-{i}")
                with lock:
                    results.append(record["execution_id"])
            except Exception as exc:
                with lock:
                    errors.append((i, exc))

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual([], errors)
        self.assertEqual(8, len(results))


if __name__ == "__main__":
    unittest.main()
