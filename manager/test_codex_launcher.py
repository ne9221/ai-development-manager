import json
import queue
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from manager.codex_launcher import CodexLaunchError, CodexLauncher, LaunchRequest, resolve_codex_executable


_END = object()


class QueueStream:
    def __init__(self): self.items = queue.Queue()
    def put(self, value): self.items.put(value)
    def close(self): self.items.put(_END)
    def __iter__(self): return self
    def __next__(self):
        value = self.items.get()
        if value is _END: raise StopIteration
        return value


class FakeStdin:
    def __init__(self, process): self.process = process
    def write(self, value): self.process.receive(json.loads(value)); return len(value)
    def flush(self): pass


class FakeProcess:
    next_pid = 4100

    def __init__(self, handler=None):
        FakeProcess.next_pid += 1
        self.pid = FakeProcess.next_pid
        self.handler = handler or happy_handler
        self.stdout, self.stderr = QueueStream(), QueueStream()
        self.stdin = FakeStdin(self)
        self.returncode = None
        self.messages = []
        self.terminate_count = self.kill_count = 0

    def receive(self, message):
        self.messages.append(message)
        self.handler(self, message)

    def reply(self, request, result=None, error=None):
        message = {"id": request["id"]}
        if error is not None: message["error"] = error
        else: message["result"] = {} if result is None else result
        self.stdout.put(json.dumps(message) + "\n")

    def notify(self, method, params): self.stdout.put(json.dumps({"method": method, "params": params}) + "\n")
    def poll(self): return self.returncode
    def terminate(self): self.terminate_count += 1; self.exit(-15)
    def kill(self): self.kill_count += 1; self.exit(-9)
    def wait(self, timeout=None):
        if self.returncode is None: raise subprocess.TimeoutExpired("codex", timeout)
        return self.returncode
    def exit(self, code):
        if self.returncode is None:
            self.returncode = code
            self.stdout.close(); self.stderr.close()


def happy_handler(process, message):
    method = message.get("method")
    if method == "initialize": process.reply(message, {"serverInfo": {"name": "codex"}})
    elif method == "thread/start": process.reply(message, {"thread": {"id": "thread-1", "path": "C:/sessions/thread-1.jsonl"}})
    elif method == "turn/start": process.reply(message, {"turn": {"id": "turn-1", "status": "inProgress"}})
    elif method == "turn/interrupt": process.reply(message, {})


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = str(Path(self.temp.name).resolve())
        self.process = None

    def tearDown(self):
        if self.process and self.process.poll() is None: self.process.exit(0)
        self.temp.cleanup()

    def launcher(self, handler=happy_handler):
        self.process = FakeProcess(handler)
        return CodexLauncher(executable=__file__, popen=lambda *args, **kwargs: self.process)

    def request(self, timeout=0.3): return LaunchRequest(self.cwd, model="gpt-test", reasoning_effort="medium", timeout_seconds=timeout)

    def prepare(self, handler=happy_handler): return self.launcher(handler).prepare(self.request())

    def test_resolve_executable_prefers_explicit_path(self):
        self.assertEqual(str(Path(__file__).resolve()), resolve_codex_executable(__file__))

    def test_initialize_success_and_prepare_does_not_start_turn(self):
        prepared = self.prepare()
        self.assertEqual("thread-1", prepared.thread_id)
        self.assertEqual(self.process.pid, prepared.pid)
        self.assertEqual(["initialize", "initialized", "thread/start"], [item["method"] for item in self.process.messages])

    def test_initialize_failure_closes_process(self):
        def handler(process, message):
            if message.get("method") == "initialize": process.reply(message, error={"code": -1, "message": "no"})
        with self.assertRaisesRegex(CodexLaunchError, "initialize"):
            self.prepare(handler)
        self.assertEqual(1, self.process.terminate_count)

    def test_thread_start_success_parses_id_and_path(self):
        prepared = self.prepare()
        self.assertEqual("thread-1", prepared.thread_id)
        self.assertEqual("C:/sessions/thread-1.jsonl", prepared.session_path)
        self.assertRegex(prepared.prepared_at, r"Z$")

    def test_thread_start_failure_closes_process(self):
        def handler(process, message):
            if message.get("method") == "initialize": process.reply(message, {})
            elif message.get("method") == "thread/start": process.reply(message, error={"code": 9, "message": "bad cwd"})
        with self.assertRaisesRegex(CodexLaunchError, "thread/start"):
            self.prepare(handler)
        self.assertEqual(1, self.process.terminate_count)

    def test_thread_id_is_required(self):
        def handler(process, message):
            if message.get("method") == "initialize": process.reply(message, {})
            elif message.get("method") == "thread/start": process.reply(message, {"thread": {}})
        with self.assertRaisesRegex(CodexLaunchError, "no thread id"):
            self.prepare(handler)

    def test_turn_start_success_maps_prompt_and_effort(self):
        launcher = self.launcher(); prepared = launcher.prepare(self.request())
        running = launcher.start(prepared, "Do bounded work")
        self.assertEqual("turn-1", running.turn_id)
        turn = next(item for item in self.process.messages if item["method"] == "turn/start")
        self.assertEqual("Do bounded work", turn["params"]["input"][0]["text"])
        self.assertEqual("medium", turn["params"]["effort"])

    def test_turn_completed(self):
        launcher = self.launcher(); running = launcher.start(launcher.prepare(self.request()), "work")
        self.process.notify("turn/completed", {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}})
        self.assertEqual("completed", launcher.wait(running).status)

    def test_turn_failed(self):
        launcher = self.launcher(); running = launcher.start(launcher.prepare(self.request()), "work")
        self.process.notify("turn/completed", {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "failed", "error": {"message": "provider failed"}}})
        outcome = launcher.wait(running)
        self.assertEqual("failed", outcome.status); self.assertEqual("turn_failed", outcome.failure_classification)

    def test_malformed_json_is_protocol_error(self):
        launcher = self.launcher(); running = launcher.start(launcher.prepare(self.request()), "work")
        self.process.stdout.put("not-json\n")
        with self.assertRaisesRegex(CodexLaunchError, "malformed JSON"):
            launcher.wait(running)

    def test_stdout_eof_without_process_exit(self):
        launcher = self.launcher(); running = launcher.start(launcher.prepare(self.request()), "work")
        self.process.stdout.close()
        with self.assertRaisesRegex(CodexLaunchError, "stdout_eof"):
            launcher.wait(running)

    def test_process_crash(self):
        launcher = self.launcher(); running = launcher.start(launcher.prepare(self.request()), "work")
        self.process.exit(17)
        with self.assertRaisesRegex(CodexLaunchError, "process_exit"):
            launcher.wait(running)

    def test_request_timeout(self):
        def handler(process, message): pass
        with self.assertRaisesRegex(CodexLaunchError, "timeout"):
            self.launcher(handler).prepare(self.request(timeout=0.02))

    def test_wait_timeout(self):
        launcher = self.launcher(); running = launcher.start(launcher.prepare(self.request(timeout=0.02)), "work")
        with self.assertRaisesRegex(CodexLaunchError, "completion timed out"):
            launcher.wait(running)

    def test_stderr_is_continuously_drained_and_bounded(self):
        prepared = self.prepare()
        self.process.stderr.put("x" * 20000)
        self.process.stderr.close()
        for _ in range(100):
            if len(prepared._client._stderr_tail) == 8192: break
            threading.Event().wait(0.001)
        self.assertEqual(8192, len(prepared._client._stderr_tail))

    def test_cancel_is_idempotent_and_reason_is_not_transmitted(self):
        launcher = self.launcher(); running = launcher.start(launcher.prepare(self.request()), "work")
        launcher.cancel(running, "secret=do-not-send"); launcher.cancel(running, "again")
        interrupts = [item for item in self.process.messages if item["method"] == "turn/interrupt"]
        self.assertEqual(1, len(interrupts)); self.assertNotIn("secret", json.dumps(interrupts))

    def test_close_terminate_is_idempotent(self):
        launcher = self.launcher(); prepared = launcher.prepare(self.request())
        launcher.close(prepared); launcher.close(prepared)
        self.assertEqual(1, self.process.terminate_count)


if __name__ == "__main__":
    unittest.main()
