import io
import json
import logging
import os
import unittest
from unittest.mock import patch

from cloud.app import create_app, logger
from manager.tasks import TaskError


def contract(**changes):
    value = {
        "contract_version": "1.0", "project": {"project_id": "adm"}, "request_type": "new_task",
        "active_task": None, "latest_handoff_summary": None, "recommended_provider": "codex",
        "mode": "code", "effort": "medium", "estimated_minutes": 12, "split_recommended": False,
        "alternatives": ["claude"], "quota_summary": "80% remaining; official; fresh",
        "quota_freshness": "fresh", "warnings": [], "next_action": "Copy prompt",
        "generated_prompt": "safe prompt", "execution_batches": [],
    }
    value.update(changes)
    return value


class DummyService:
    def files(self): return object()


def invoke(app, method, path, payload=None, auth="Bearer test-secret", query=""):
    raw = b"" if payload is None else (payload if isinstance(payload, bytes) else json.dumps(payload).encode())
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query, "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
    if auth is not None: environ["HTTP_AUTHORIZATION"] = auth
    captured = {}
    def start(status, headers): captured.update(status=status, headers=dict(headers))
    body = b"".join(app(environ, start))
    return int(captured["status"].split()[0]), json.loads(body), captured["headers"]


class CloudAppTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ADM_API_KEY": "test-secret"}); self.env.start()
        self.calls = []
        def bridge(store, service, request, read_only=False):
            self.calls.append((request, read_only)); return contract(request_type="scheduling" if request.get("multi_task") else "continuation" if request.get("task_id") else "new_task")
        self.app = create_app(lambda: DummyService(), bridge)
    def tearDown(self): self.env.stop()

    def test_health_is_minimal_and_public(self):
        self.assertEqual(logging.INFO, logger.getEffectiveLevel())
        self.assertTrue(logger.handlers)
        status, body, _ = invoke(self.app, "GET", "/health", auth=None)
        self.assertEqual(200, status)
        self.assertEqual({"status", "contract_version", "timestamp", "service", "revision", "configuration", "git_sha"}, set(body))
        self.assertEqual("1.0", body["contract_version"])

    def test_health_reports_revision_provenance_when_env_is_set(self):
        with patch.dict(os.environ, {"K_SERVICE": "adm-runtime-bridge", "K_REVISION": "adm-runtime-bridge-00016-rek",
                                     "K_CONFIGURATION": "adm-runtime-bridge", "ADM_GIT_SHA": "c0b5d8a"}):
            status, body, _ = invoke(self.app, "GET", "/health", auth=None)
        self.assertEqual(200, status)
        self.assertEqual("adm-runtime-bridge", body["service"])
        self.assertEqual("adm-runtime-bridge-00016-rek", body["revision"])
        self.assertEqual("adm-runtime-bridge", body["configuration"])
        self.assertEqual("c0b5d8a", body["git_sha"])

    def test_health_falls_back_to_null_when_provenance_env_is_missing_and_stays_200(self):
        with patch.dict(os.environ, {}, clear=True):
            status, body, _ = invoke(self.app, "GET", "/health", auth=None)
        self.assertEqual(200, status)
        self.assertIsNone(body["service"]); self.assertIsNone(body["revision"])
        self.assertIsNone(body["configuration"]); self.assertIsNone(body["git_sha"])
        self.assertEqual("ok", body["status"])

    def test_health_falls_back_to_null_when_provenance_env_is_empty_string(self):
        with patch.dict(os.environ, {"K_SERVICE": "", "K_REVISION": "", "K_CONFIGURATION": "", "ADM_GIT_SHA": ""}):
            status, body, _ = invoke(self.app, "GET", "/health", auth=None)
        self.assertEqual(200, status)
        self.assertIsNone(body["service"]); self.assertIsNone(body["revision"])
        self.assertIsNone(body["configuration"]); self.assertIsNone(body["git_sha"])

    def test_health_provenance_never_leaks_secret_looking_values(self):
        with patch.dict(os.environ, {"ADM_GIT_SHA": "token=hunter2"}):
            status, body, _ = invoke(self.app, "GET", "/health", auth=None)
        self.assertEqual(200, status)
        self.assertNotIn("hunter2", json.dumps(body))
        self.assertIn("[REDACTED]", body["git_sha"])

    def test_health_does_not_require_auth_or_change_dispatch_contract(self):
        status, _, _ = invoke(self.app, "GET", "/health", auth=None)
        self.assertEqual(200, status)
        status, body, _ = invoke(self.app, "POST", "/dispatch", {"user_request": "work"}, auth=None)
        self.assertEqual(401, status); self.assertEqual("auth_failure", body["error"]["code"])

    def test_valid_alias_continuation_and_multitask_dispatch(self):
        for payload, kind in [
            ({"project_id": "ADM", "user_request": "new"}, "new_task"),
            ({"project_id": "alias", "task_id": "t1", "user_request": "continue"}, "continuation"),
            ({"project_id": "adm", "user_request": "schedule", "multi_task": True}, "scheduling")]:
            status, body, _ = invoke(self.app, "POST", "/dispatch", payload)
            self.assertEqual(200, status); self.assertEqual(kind, body["request_type"])
        self.assertTrue(all(read_only for _, read_only in self.calls))

    def test_stale_warning_and_contract_preserved(self):
        app = create_app(lambda: DummyService(), lambda *args, **kwargs: contract(quota_freshness="stale", warnings=["quota stale"]))
        status, body, _ = invoke(app, "POST", "/dispatch", {"user_request": "work"})
        self.assertEqual(200, status); self.assertEqual("1.0", body["contract_version"]); self.assertIn("quota stale", body["warnings"])

    def test_invalid_auth_and_query_secret_not_accepted(self):
        self.assertEqual(401, invoke(self.app, "POST", "/dispatch", {"user_request": "work"}, auth=None, query="api_key=test-secret")[0])
        self.assertEqual(401, invoke(self.app, "POST", "/dispatch", {"user_request": "work"}, auth="Bearer wrong")[0])
        self.assertFalse(self.calls)

    def test_malformed_request(self):
        self.assertEqual(400, invoke(self.app, "POST", "/dispatch", b"not-json")[0])
        self.assertEqual(400, invoke(self.app, "POST", "/dispatch", {"user_request": "work", "token": "bad"})[0])

    def test_drive_unavailable_and_project_alias_failure(self):
        broken = create_app(lambda: (_ for _ in ()).throw(OSError("credential=private")), lambda *args, **kwargs: contract())
        status, body, _ = invoke(broken, "POST", "/dispatch", {"user_request": "work"})
        self.assertEqual(503, status); self.assertEqual("drive_unavailable", body["error"]["code"]); self.assertNotIn("private", json.dumps(body))
        missing = create_app(lambda: DummyService(), lambda *args, **kwargs: (_ for _ in ()).throw(TaskError("project resolution expected one match; found 0")))
        self.assertEqual(404, invoke(missing, "POST", "/dispatch", {"user_request": "work"})[0])

    def test_response_and_logs_are_sanitized(self):
        app = create_app(lambda: DummyService(), lambda *args, **kwargs: contract(generated_prompt="token=hunter2", quota_summary="safe"))
        with self.assertLogs("runtime_bridge_cloud", logging.INFO) as logs:
            _, body, _ = invoke(app, "POST", "/dispatch", {"project_id": "adm", "user_request": "secret business request"}, auth="Bearer test-secret")
        serialized = json.dumps(body); logged = "".join(logs.output)
        self.assertNotIn("hunter2", serialized); self.assertNotIn("test-secret", logged); self.assertNotIn("secret business request", logged); self.assertNotIn("generated_prompt", logged)

    def test_runtime_exception_and_contract_mismatch(self):
        broken = create_app(lambda: DummyService(), lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("token=private")))
        self.assertEqual(500, invoke(broken, "POST", "/dispatch", {"user_request": "work"})[0])
        mismatch = create_app(lambda: DummyService(), lambda *args, **kwargs: contract(contract_version="2.0"))
        self.assertEqual("contract_mismatch", invoke(mismatch, "POST", "/dispatch", {"user_request": "work"})[1]["error"]["code"])


if __name__ == "__main__": unittest.main()
