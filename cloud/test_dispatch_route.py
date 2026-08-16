import os
import unittest
from unittest.mock import patch

from cloud.app import DISPATCH_INGRESS_PATH, create_app
from cloud.dispatch_ingress import DispatchIngressError
from cloud.test_app import DummyService, contract, invoke
from manager.tasks import TaskError


def accepted(**changes):
    value = {"accepted": True, "request_id": "req-1", "task_id": "dispatch-req-1",
              "command_id": "dispatch-req-1", "status": "queued"}
    value.update(changes)
    return value


class DispatchRouteTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ADM_API_KEY": "test-secret"}); self.env.start()
        self.calls = []

        def ingress(store, service, lock_registry_factory, payload):
            self.calls.append(payload)
            return accepted()

        self.ingress = ingress
        self.app = create_app(
            lambda: DummyService(), lambda *a, **k: contract(),
            write_service_factory=lambda: DummyService(),
            lock_registry_factory=lambda project_id, request_id: object(),
            dispatch_ingress_func=self.ingress,
        )

    def tearDown(self): self.env.stop()

    def test_valid_request_returns_accepted_contract(self):
        status, body, _ = invoke(self.app, "POST", DISPATCH_INGRESS_PATH, {"request_id": "req-1", "project_id": "p1", "title": "t", "goal": "g"})
        self.assertEqual(200, status)
        self.assertEqual(accepted(), body)
        self.assertEqual(1, len(self.calls))

    def test_missing_auth_rejected(self):
        status, _, _ = invoke(self.app, "POST", DISPATCH_INGRESS_PATH, {"request_id": "req-1", "project_id": "p1", "title": "t", "goal": "g"}, auth=None)
        self.assertEqual(401, status)
        self.assertEqual([], self.calls)

    def test_invalid_auth_rejected(self):
        status, _, _ = invoke(self.app, "POST", DISPATCH_INGRESS_PATH, {"request_id": "req-1", "project_id": "p1", "title": "t", "goal": "g"}, auth="Bearer wrong")
        self.assertEqual(401, status)
        self.assertEqual([], self.calls)

    def test_service_unconfigured_when_no_api_key(self):
        self.env.stop()
        try:
            with patch.dict(os.environ, {}, clear=True):
                status, body, _ = invoke(self.app, "POST", DISPATCH_INGRESS_PATH, {"request_id": "req-1", "project_id": "p1", "title": "t", "goal": "g"})
                self.assertEqual(503, status)
                self.assertEqual("service_unconfigured", body["error"]["code"])
        finally:
            self.env.start()

    def test_malformed_json_body_rejected(self):
        status, body, _ = invoke(self.app, "POST", DISPATCH_INGRESS_PATH, b"not-json")
        self.assertEqual(400, status)
        self.assertEqual("malformed_request", body["error"]["code"])
        self.assertEqual([], self.calls)

    def test_ingress_validation_error_maps_to_correct_status(self):
        def raise_malformed(store, service, lock_registry_factory, payload):
            raise DispatchIngressError("malformed_request", "title is required")
        app = create_app(lambda: DummyService(), lambda *a, **k: {}, write_service_factory=lambda: DummyService(),
                          lock_registry_factory=lambda p, r: object(), dispatch_ingress_func=raise_malformed)
        status, body, _ = invoke(app, "POST", DISPATCH_INGRESS_PATH, {"request_id": "req-1", "project_id": "p1", "title": "", "goal": "g"})
        self.assertEqual(400, status); self.assertEqual("malformed_request", body["error"]["code"])

    def test_unknown_project_maps_to_404(self):
        def raise_unknown(store, service, lock_registry_factory, payload):
            raise DispatchIngressError("unknown_project", "unknown project: p9")
        app = create_app(lambda: DummyService(), lambda *a, **k: {}, write_service_factory=lambda: DummyService(),
                          lock_registry_factory=lambda p, r: object(), dispatch_ingress_func=raise_unknown)
        status, body, _ = invoke(app, "POST", DISPATCH_INGRESS_PATH, {"request_id": "req-1", "project_id": "p9", "title": "t", "goal": "g"})
        self.assertEqual(404, status); self.assertEqual("unknown_project", body["error"]["code"])

    def test_write_drive_unavailable_maps_to_503(self):
        def broken_write_service():
            raise OSError("credential=private")
        app = create_app(lambda: DummyService(), lambda *a, **k: {}, write_service_factory=broken_write_service,
                          lock_registry_factory=lambda p, r: object(), dispatch_ingress_func=self.ingress)
        status, body, _ = invoke(app, "POST", DISPATCH_INGRESS_PATH, {"request_id": "req-1", "project_id": "p1", "title": "t", "goal": "g"})
        self.assertEqual(503, status); self.assertEqual("drive_unavailable", body["error"]["code"])
        self.assertNotIn("private", str(body))

    def test_unexpected_exception_maps_to_500_and_is_sanitized(self):
        def raise_unexpected(store, service, lock_registry_factory, payload):
            raise RuntimeError("token=hunter2")
        app = create_app(lambda: DummyService(), lambda *a, **k: {}, write_service_factory=lambda: DummyService(),
                          lock_registry_factory=lambda p, r: object(), dispatch_ingress_func=raise_unexpected)
        status, body, _ = invoke(app, "POST", DISPATCH_INGRESS_PATH, {"request_id": "req-1", "project_id": "p1", "title": "t", "goal": "g"})
        self.assertEqual(500, status)
        self.assertNotIn("hunter2", str(body))

    def test_generic_task_error_maps_to_422(self):
        def raise_task_error(store, service, lock_registry_factory, payload):
            raise TaskError("invalid command: schema mismatch")
        app = create_app(lambda: DummyService(), lambda *a, **k: {}, write_service_factory=lambda: DummyService(),
                          lock_registry_factory=lambda p, r: object(), dispatch_ingress_func=raise_task_error)
        status, body, _ = invoke(app, "POST", DISPATCH_INGRESS_PATH, {"request_id": "req-1", "project_id": "p1", "title": "t", "goal": "g"})
        self.assertEqual(422, status)

    def test_existing_dispatch_route_is_unaffected(self):
        status, body, _ = invoke(self.app, "POST", "/dispatch", {"user_request": "work"})
        self.assertEqual(200, status)
        self.assertEqual(contract(), body)
        self.assertEqual([], self.calls)

    def test_no_direct_provider_spawn_from_http_handler(self):
        """The route module must never import execution/launcher machinery --
        only the existing ingress/dispatcher/persistence layers."""
        import cloud.app as app_module
        with open(app_module.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("execution_runner", "claude_launcher", "codex_launcher", "subprocess", "Popen"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
