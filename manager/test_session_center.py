import argparse
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from manager.session_center import (
    LiveSession, SessionCenterError, SessionView, build_pending, drive_status_source, file_status_source,
    handler_for, load_execution, read_codex_meta, resolve_session, wait_for_execution,
)


def bootstrap_args(**overrides):
    base = {
        "provider_session_id": None, "execution_file": None, "execution_project_id": None,
        "execution_id": None, "wait_seconds": 5.0, "project_id": None, "task_id": None,
        "branch": None, "port": 0, "idle_seconds": 15.0,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class SessionCenterTest(unittest.TestCase):
    def session_file(self, directory: str) -> Path:
        path = Path(directory) / "session.jsonl"
        path.write_text(json.dumps({
            "timestamp": "2026-08-14T09:31:49.290Z", "type": "session_meta",
            "payload": {"id": "provider-1", "cwd": directory},
        }) + "\n", encoding="utf-8")
        return path

    def test_live_snapshot_tracks_file_growth_without_transcript_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.session_file(directory)
            meta = read_codex_meta(path, "provider-1")
            session = LiveSession("provider-1", path, meta["cwd"], meta["started_at"], "adm", "task-1", None, "branch", idle_seconds=.01)
            time.sleep(.02)
            self.assertEqual(session.snapshot()["current_state"], "waiting")
            with path.open("a", encoding="utf-8") as handle:
                handle.write("activity\n")
            snapshot = session.snapshot()
            self.assertEqual(snapshot["current_state"], "running")
            self.assertEqual(snapshot["execution_id"], "UNLINKED")

    def test_execution_requires_exact_provider_and_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution.json"
            record = {
                "provider": "codex", "project_id": "adm", "task_id": "task-1", "execution_id": "run-1",
                "provider_session_id": "provider-1", "task_snapshot": {"working_directory": directory, "branch": "main"},
            }
            path.write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual(load_execution(path, "provider-1", directory)["execution_id"], "run-1")
            with self.assertRaisesRegex(SessionCenterError, "provider_session_id"):
                load_execution(path, "provider-2", directory)

    def test_wait_for_execution_requires_native_session_link(self):
        linked = {"execution_id": "run-1", "provider_session_id": "provider-1"}
        class Store:
            calls = 0
            def get(self, *_args):
                self.calls += 1
                return {} if self.calls == 1 else linked
        self.assertIs(wait_for_execution(Store(), "adm", "run-1", 1), linked)

    def linked_session(self, directory: str, status_source):
        path = self.session_file(directory)
        meta = read_codex_meta(path, "provider-1")
        return LiveSession(
            "provider-1", path, meta["cwd"], meta["started_at"], "adm", "task-1", "run-1", "branch",
            idle_seconds=.01, correlated=True, status_source=status_source,
        )

    def test_completed_execution_with_idle_transcript_shows_completed_not_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.linked_session(directory, lambda: "completed")
            time.sleep(.02)
            snapshot = session.snapshot()
            self.assertEqual("completed", snapshot["current_state"])
            self.assertNotEqual("waiting", snapshot["current_state"])

    def test_terminal_statuses_are_shown_verbatim_not_translated_to_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            for status in ("failed", "cancelled", "interrupted"):
                session = self.linked_session(directory, lambda status=status: status)
                time.sleep(.02)
                self.assertEqual(status, session.snapshot()["current_state"])

    def test_active_execution_with_recent_activity_shows_running(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.linked_session(directory, lambda: "running")
            self.assertEqual("running", session.snapshot()["current_state"])

    def test_active_execution_with_idle_transcript_shows_waiting_not_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.linked_session(directory, lambda: "running")
            time.sleep(.02)
            self.assertEqual("waiting", session.snapshot()["current_state"])

    def test_unlinked_session_never_fakes_a_terminal_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.session_file(directory)
            meta = read_codex_meta(path, "provider-1")
            session = LiveSession("provider-1", path, meta["cwd"], meta["started_at"], "adm", "task-1", None, "branch", idle_seconds=.01)
            time.sleep(.02)
            snapshot = session.snapshot()
            self.assertEqual("waiting", snapshot["current_state"])
            self.assertNotIn(snapshot["current_state"], ("completed", "failed", "cancelled", "interrupted"))
            self.assertFalse(snapshot["correlated"])

    def test_unknown_or_unavailable_authoritative_status_falls_back_to_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.linked_session(directory, lambda: None)
            time.sleep(.02)
            self.assertEqual("waiting", session.snapshot()["current_state"])
            with session.session_file.open("a", encoding="utf-8") as handle:
                handle.write("activity\n")
            self.assertEqual("running", session.snapshot()["current_state"])

    def test_drive_status_source_degrades_to_none_on_missing_or_broken_record(self):
        class MissingStore:
            def get(self, *_args):
                raise KeyError("no such record")
        self.assertIsNone(drive_status_source(MissingStore(), "adm", "run-1")())

        class ErrorStore:
            def get(self, *_args):
                raise RuntimeError("expected one Drive record for executions/adm/run-1; found 0")
        self.assertIsNone(drive_status_source(ErrorStore(), "adm", "run-1")())

        class OkStore:
            def get(self, *_args):
                return {
                    "status": "completed", "access": "read_only",
                    "cleanup_evidence": {"task_claim_release": "released", "writer_release": "not_required"},
                }
        self.assertEqual("completed", drive_status_source(OkStore(), "adm", "run-1")())

    def test_file_status_source_rereads_status_and_degrades_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            execution_path = Path(directory) / "execution.json"
            record = {
                "provider": "codex", "project_id": "adm", "task_id": "task-1", "execution_id": "run-1",
                "provider_session_id": "provider-1", "status": "running",
                "task_snapshot": {"working_directory": directory, "branch": "main"},
            }
            execution_path.write_text(json.dumps(record), encoding="utf-8")
            source = file_status_source(execution_path, "provider-1", directory)
            self.assertEqual("running", source())
            execution_path.write_text(json.dumps({
                **record, "status": "completed", "access": "production_write",
                "cleanup_evidence": {"task_claim_release": "released", "writer_release": "released"},
            }), encoding="utf-8")
            self.assertEqual("completed", source())
            execution_path.write_text("not json", encoding="utf-8")
            self.assertIsNone(source())

    class _RecordStore:
        def __init__(self, record):
            self.record = record

        def get(self, *_args):
            return self.record

    def _state_of(self, record):
        return drive_status_source(self._RecordStore(record), "adm", "run-1")()

    def test_completed_with_claim_still_held_is_not_shown_as_completed(self):
        record = {"status": "completed", "access": "read_only",
                  "cleanup_evidence": {"task_claim_release": "retained", "writer_release": "not_required"}}
        self.assertEqual("finishing", self._state_of(record))

    def test_read_only_completed_with_claim_released_and_writer_not_required_shows_completed(self):
        record = {"status": "completed", "access": "read_only",
                  "cleanup_evidence": {"task_claim_release": "released", "writer_release": "not_required"}}
        self.assertEqual("completed", self._state_of(record))

    def test_production_write_completed_with_claim_and_writer_released_shows_completed(self):
        record = {"status": "completed", "access": "production_write",
                  "cleanup_evidence": {"task_claim_release": "released", "writer_release": "released"}}
        self.assertEqual("completed", self._state_of(record))

    def test_production_write_completed_with_writer_not_released_is_not_shown_as_completed(self):
        for writer_release in ("retained", "not_required", "failed", None):
            with self.subTest(writer_release=writer_release):
                record = {"status": "completed", "access": "production_write",
                          "cleanup_evidence": {"task_claim_release": "released", "writer_release": writer_release}}
                self.assertEqual("finishing", self._state_of(record))

    def test_all_terminal_statuses_are_gated_by_cleanup_confirmation(self):
        unconfirmed = {"task_claim_release": "retained", "writer_release": "not_required"}
        confirmed = {"task_claim_release": "released", "writer_release": "not_required"}
        for status in ("completed", "failed", "interrupted", "cancelled"):
            with self.subTest(status=status):
                self.assertEqual(
                    "finishing", self._state_of({"status": status, "access": "read_only", "cleanup_evidence": unconfirmed})
                )
                self.assertEqual(
                    status, self._state_of({"status": status, "access": "read_only", "cleanup_evidence": confirmed})
                )

    def test_legacy_or_malformed_cleanup_evidence_fails_closed(self):
        for evidence in (None, {}, "not-a-dict", 42, []):
            with self.subTest(evidence=repr(evidence)):
                record = {"status": "completed", "access": "read_only", "cleanup_evidence": evidence}
                self.assertEqual("finishing", self._state_of(record))
        legacy = {"status": "completed", "access": "read_only"}  # predates the cleanup_evidence field
        self.assertEqual("finishing", self._state_of(legacy))

    def test_missing_or_unrecognized_access_defaults_to_the_stricter_writer_gate(self):
        lenient_evidence = {"task_claim_release": "released", "writer_release": "not_required"}
        for access in (None, "", "something_unexpected"):
            with self.subTest(access=access):
                record = {"status": "completed", "access": access, "cleanup_evidence": lenient_evidence}
                self.assertEqual("finishing", self._state_of(record))
        strict_evidence = {"task_claim_release": "released", "writer_release": "released"}
        record = {"status": "completed", "access": None, "cleanup_evidence": strict_evidence}
        self.assertEqual("completed", self._state_of(record))

    def test_snapshot_reflects_the_persist_terminal_before_cleanup_execution_race_window(self):
        """Reproduces the exact window Claude 1号's review flagged: terminalize_execution()
        writes execution.status as terminal and a "retained" cleanup_evidence first, then
        cleanup_execution() overwrites it with "released" only once authority is actually
        released. Session Center must not show completed until the second write lands."""
        with tempfile.TemporaryDirectory() as directory:
            store = self._RecordStore({
                "status": "completed", "access": "production_write",
                "cleanup_evidence": {"task_claim_release": "retained", "writer_release": "retained"},
            })
            session = self.linked_session(directory, drive_status_source(store, "adm", "run-1"))
            self.assertEqual("finishing", session.snapshot()["current_state"])
            store.record = {
                **store.record,
                "cleanup_evidence": {"task_claim_release": "released", "writer_release": "released"},
            }
            self.assertEqual("completed", session.snapshot()["current_state"])

    def test_health_endpoint_is_independent_of_drive_and_correlation_state(self):
        """The watcher's launch gate must be able to check liveness alone,
        without that check depending on Drive reachability or on whether
        this particular session ever correlates -- those stay /api/session's
        job. An UNLINKED, uncorrelated session must still answer /health."""
        with tempfile.TemporaryDirectory() as directory:
            path = self.session_file(directory)
            meta = read_codex_meta(path, "provider-1")
            session = LiveSession("provider-1", path, meta["cwd"], meta["started_at"], "adm", "task-1", None, "branch")
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(session))
            port = server.server_address[1]
            import threading
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
                    self.assertEqual(200, response.status)
                    self.assertEqual({"status": "ok"}, json.loads(response.read()))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class BootstrapTests(unittest.TestCase):
    """Session Center must bind /health before any Execution/session
    correlation is even attempted -- otherwise a watcher that gates new
    launches on /health can never launch the very Execution this process is
    waiting to observe (the livelock the reviewer found)."""

    def session_file(self, directory: str) -> Path:
        path = Path(directory) / "session.jsonl"
        path.write_text(json.dumps({
            "timestamp": "2026-08-14T09:31:49.290Z", "type": "session_meta",
            "payload": {"id": "provider-1", "cwd": directory},
        }) + "\n", encoding="utf-8")
        return path

    def start_server(self, view):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(view))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, port

    def get(self, port, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_server_binds_and_health_is_ok_before_any_execution_exists(self):
        args = bootstrap_args(execution_project_id="adm", execution_id="run-1")
        view = SessionView(build_pending(args))
        server, port = self.start_server(view)
        try:
            status, body = self.get(port, "/health")
            self.assertEqual(200, status)
            self.assertEqual({"status": "ok"}, body)
        finally:
            server.shutdown(); server.server_close()

    def test_api_session_shows_correlating_and_never_fakes_unknown_fields(self):
        args = bootstrap_args(execution_project_id="adm", execution_id="run-1")
        view = SessionView(build_pending(args))
        server, port = self.start_server(view)
        try:
            status, body = self.get(port, "/api/session")
            self.assertEqual(200, status)
            self.assertEqual("correlating", body["current_state"])
            self.assertFalse(body["correlated"])
            self.assertEqual("run-1", body["execution_id"])
            self.assertEqual("adm", body["project_id"])
            self.assertIsNone(body["task_id"])
            for field in ("provider_session_id", "cwd", "branch", "started_at", "latest_activity"):
                self.assertIsNone(body[field], f"{field} must not be faked while correlating")
        finally:
            server.shutdown(); server.server_close()

    def test_provider_session_mode_also_shows_correlating_not_a_startup_failure(self):
        args = bootstrap_args(provider_session_id="provider-1", project_id="adm", task_id="task-1")
        view = SessionView(build_pending(args))
        server, port = self.start_server(view)
        try:
            status, body = self.get(port, "/api/session")
            self.assertEqual(200, status)
            self.assertEqual("correlating", body["current_state"])
            self.assertFalse(body["correlated"])
        finally:
            server.shutdown(); server.server_close()

    def test_execution_appearing_later_transitions_the_same_process_to_correlated(self):
        with tempfile.TemporaryDirectory() as codex_home, tempfile.TemporaryDirectory() as cwd:
            sessions_dir = Path(codex_home) / "sessions"
            sessions_dir.mkdir()
            session_path = sessions_dir / "rollout-provider-1.jsonl"
            session_path.write_text(json.dumps({
                "timestamp": "2026-08-14T09:31:49.290Z", "type": "session_meta",
                "payload": {"id": "provider-1", "cwd": cwd},
            }) + "\n", encoding="utf-8")
            record = {
                "provider": "codex", "project_id": "adm", "task_id": "task-1", "execution_id": "run-1",
                "provider_session_id": "provider-1", "task_snapshot": {"working_directory": cwd, "branch": "main"},
            }

            class DelayedStore:
                def __init__(self):
                    self.calls = 0

                def get(self, area, project_id, execution_id):
                    self.calls += 1
                    if self.calls < 3:
                        raise KeyError("not yet")
                    return record

            args = bootstrap_args(execution_project_id="adm", execution_id="run-1", wait_seconds=5.0)
            view = SessionView(build_pending(args))
            server, port = self.start_server(view)
            try:
                status, body = self.get(port, "/api/session")
                self.assertEqual("correlating", body["current_state"], "must be visible before correlation resolves")

                delayed_store = DelayedStore()
                with patch.dict("os.environ", {"CODEX_HOME": codex_home}), \
                     patch("manager.tasks.build_service", return_value=object()), \
                     patch("manager.tasks.DriveRecords", return_value=delayed_store):
                    live_session = resolve_session(args, time.monotonic() + 5.0)
                view.resolve(live_session)  # exactly what _resolve_and_swap does on success

                status, body = self.get(port, "/api/session")
                self.assertEqual(200, status)
                self.assertTrue(body["correlated"])
                self.assertEqual("provider-1", body["provider_session_id"])
                self.assertNotEqual("correlating", body["current_state"])
                self.assertGreaterEqual(delayed_store.calls, 3, "must have retried, not failed on first miss")
            finally:
                server.shutdown(); server.server_close()

    def test_resolution_timeout_reports_failure_without_crashing_the_server(self):
        args = bootstrap_args(execution_project_id="adm", execution_id="run-timeout", wait_seconds=0.3)
        view = SessionView(build_pending(args))
        server, port = self.start_server(view)
        try:

            class NeverStore:
                def get(self, area, project_id, execution_id):
                    raise KeyError("never appears")

            with patch("manager.tasks.build_service", return_value=object()), \
                 patch("manager.tasks.DriveRecords", return_value=NeverStore()):
                thread = threading.Thread(target=self._run_and_fail, args=(view, args), daemon=True)
                thread.start()
                thread.join(timeout=5)

            status, body = self.get(port, "/api/session")
            self.assertEqual(200, status, "server must still be up and answering after a resolution timeout")
            self.assertFalse(body["correlated"])
            self.assertNotEqual("completed", body["current_state"])
        finally:
            server.shutdown(); server.server_close()

    @staticmethod
    def _run_and_fail(view, args):
        from manager.session_center import _resolve_and_swap
        _resolve_and_swap(view, args)


if __name__ == "__main__":
    unittest.main()
