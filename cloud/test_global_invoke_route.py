import os
import unittest
from unittest.mock import patch

from cloud.app import GLOBAL_INVOKE_PATH, create_app
from cloud.dispatch_ingress import DispatchIngressError
from cloud.test_app import DummyService, contract, invoke
from manager.global_invoke import GlobalInvokeError
from manager.tasks import TaskError


def accepted(**changes):
    value = {"accepted": True, "request_id": "inv-1", "task_id": "dispatch-inv-1",
              "command_id": "dispatch-inv-1", "status": "queued"}
    value.update(changes)
    return value


def request(**changes):
    value = {"idempotency_key": "inv-1", "project": "p1", "title": "t", "goal": "g"}
    value.update(changes)
    return value


class GlobalInvokeRouteTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ADM_API_KEY": "test-secret"}); self.env.start()
        self.calls = []

        def global_invoke_func(store, service, lock_registry_factory, payload):
            self.calls.append(payload)
            return accepted()

        self.global_invoke_func = global_invoke_func
        self.app = create_app(
            lambda: DummyService(), lambda *a, **k: contract(),
            write_service_factory=lambda: DummyService(),
            lock_registry_factory=lambda project_id, request_id: object(),
            global_invoke_func=self.global_invoke_func,
        )

    def tearDown(self): self.env.stop()

    def test_valid_request_returns_accepted_contract(self):
        status, body, _ = invoke(self.app, "POST", GLOBAL_INVOKE_PATH, request())
        self.assertEqual(200, status)
        self.assertEqual(accepted(), body)
        self.assertEqual(1, len(self.calls))

    def test_missing_auth_rejected(self):
        status, _, _ = invoke(self.app, "POST", GLOBAL_INVOKE_PATH, request(), auth=None)
        self.assertEqual(401, status)
        self.assertEqual([], self.calls)

    def test_malformed_json_body_rejected(self):
        status, body, _ = invoke(self.app, "POST", GLOBAL_INVOKE_PATH, b"not-json")
        self.assertEqual(400, status)
        self.assertEqual("malformed_request", body["error"]["code"])
        self.assertEqual([], self.calls)

    def test_project_ambiguous_maps_to_409(self):
        def raise_ambiguous(store, service, lock_registry_factory, payload):
            raise GlobalInvokeError("project_ambiguous", "ambiguous alias 'x'")
        app = create_app(lambda: DummyService(), lambda *a, **k: {}, write_service_factory=lambda: DummyService(),
                          lock_registry_factory=lambda p, r: object(), global_invoke_func=raise_ambiguous)
        status, body, _ = invoke(app, "POST", GLOBAL_INVOKE_PATH, request())
        self.assertEqual(409, status); self.assertEqual("project_ambiguous", body["error"]["code"])

    def test_project_not_found_maps_to_404(self):
        def raise_unknown(store, service, lock_registry_factory, payload):
            raise GlobalInvokeError("project_not_found", "unknown project: ghost")
        app = create_app(lambda: DummyService(), lambda *a, **k: {}, write_service_factory=lambda: DummyService(),
                          lock_registry_factory=lambda p, r: object(), global_invoke_func=raise_unknown)
        status, body, _ = invoke(app, "POST", GLOBAL_INVOKE_PATH, request(project="ghost"))
        self.assertEqual(404, status); self.assertEqual("project_not_found", body["error"]["code"])

    def test_repo_write_not_eligible_maps_to_409(self):
        def raise_ineligible(store, service, lock_registry_factory, payload):
            raise GlobalInvokeError("repo_write_not_eligible", "project 'p1' is marked 'unresolved'")
        app = create_app(lambda: DummyService(), lambda *a, **k: {}, write_service_factory=lambda: DummyService(),
                          lock_registry_factory=lambda p, r: object(), global_invoke_func=raise_ineligible)
        status, body, _ = invoke(app, "POST", GLOBAL_INVOKE_PATH, request(repo_write=True, allowed_paths=["a.py"]))
        self.assertEqual(409, status); self.assertEqual("repo_write_not_eligible", body["error"]["code"])

    def test_underlying_dispatch_ingress_error_still_maps_correctly(self):
        def raise_unsafe_path(store, service, lock_registry_factory, payload):
            raise DispatchIngressError("unsafe_allowed_path", "repo_write.allowed_paths entry is unsafe")
        app = create_app(lambda: DummyService(), lambda *a, **k: {}, write_service_factory=lambda: DummyService(),
                          lock_registry_factory=lambda p, r: object(), global_invoke_func=raise_unsafe_path)
        status, body, _ = invoke(app, "POST", GLOBAL_INVOKE_PATH, request(repo_write=True, allowed_paths=["../x"]))
        self.assertEqual(422, status); self.assertEqual("unsafe_allowed_path", body["error"]["code"])

    def test_write_drive_unavailable_maps_to_503(self):
        def broken_write_service():
            raise OSError("credential=private")
        app = create_app(lambda: DummyService(), lambda *a, **k: {}, write_service_factory=broken_write_service,
                          lock_registry_factory=lambda p, r: object(), global_invoke_func=self.global_invoke_func)
        status, body, _ = invoke(app, "POST", GLOBAL_INVOKE_PATH, request())
        self.assertEqual(503, status); self.assertEqual("drive_unavailable", body["error"]["code"])
        self.assertNotIn("private", str(body))

    def test_unexpected_exception_maps_to_500_and_is_sanitized(self):
        def raise_unexpected(store, service, lock_registry_factory, payload):
            raise RuntimeError("token=hunter2")
        app = create_app(lambda: DummyService(), lambda *a, **k: {}, write_service_factory=lambda: DummyService(),
                          lock_registry_factory=lambda p, r: object(), global_invoke_func=raise_unexpected)
        status, body, _ = invoke(app, "POST", GLOBAL_INVOKE_PATH, request())
        self.assertEqual(500, status)
        self.assertNotIn("hunter2", str(body))

    def test_generic_task_error_maps_to_422(self):
        def raise_task_error(store, service, lock_registry_factory, payload):
            raise TaskError("invalid command: schema mismatch")
        app = create_app(lambda: DummyService(), lambda *a, **k: {}, write_service_factory=lambda: DummyService(),
                          lock_registry_factory=lambda p, r: object(), global_invoke_func=raise_task_error)
        status, body, _ = invoke(app, "POST", GLOBAL_INVOKE_PATH, request())
        self.assertEqual(422, status)

    def test_existing_dispatch_route_and_read_only_route_are_unaffected(self):
        status, body, _ = invoke(self.app, "POST", "/dispatch", {"user_request": "work"})
        self.assertEqual(200, status)
        self.assertEqual(contract(), body)
        self.assertEqual([], self.calls)

    def test_no_direct_provider_spawn_from_http_handler(self):
        import cloud.app as app_module
        with open(app_module.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("execution_runner", "claude_launcher", "codex_launcher", "subprocess", "Popen"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
