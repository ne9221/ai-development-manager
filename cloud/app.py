#!/usr/bin/env python3
"""Small authenticated WSGI boundary for runtime_bridge contract 1.0."""

import hmac
import io
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from wsgiref.simple_server import make_server

from cloud.dispatch_ingress import DispatchIngressError, handle_dispatch
from cloud.drive_credentials import user_oauth_write_credentials
from manager.dispatch_requests import dispatch_request_registry
from manager.global_invoke import GlobalInvokeError, global_invoke
from manager.runtime_bridge import redact, runtime_bridge
from manager.tasks import DriveRecords, TaskError


CONTRACT_VERSION = "1.0"
ALLOWED_INPUTS = {"project_id", "user_request", "task_id", "task_type", "complexity", "preferred_provider", "excluded_provider", "multi_task"}
DISPATCH_INGRESS_PATH = "/api/v1/tasks/dispatch"
GLOBAL_INVOKE_PATH = "/api/v1/tasks/global-invoke"
DISPATCH_INGRESS_ERROR_STATUS = {
    "malformed_request": "400 Bad Request",
    "unknown_project": "404 Not Found",
    "unknown_account": "404 Not Found",
    "unknown_execution": "404 Not Found",
    "retry_not_eligible": "409 Conflict",
    "idempotency_backend_unavailable": "503 Service Unavailable",
    "read_only_required": "422 Unprocessable Entity",
    "dispatch_state_inconsistent": "409 Conflict",
}
GLOBAL_INVOKE_ERROR_STATUS = {
    "malformed_request": "400 Bad Request",
    "project_not_found": "404 Not Found",
    "project_ambiguous": "409 Conflict",
    "project_disabled": "409 Conflict",
    "repo_write_not_eligible": "409 Conflict",
    "governance_missing": "409 Conflict",
    "unknown_project": "404 Not Found",
    "repo_identity_mismatch": "409 Conflict",
    "empty_allowed_paths": "400 Bad Request",
    "baseline_resolution_failed": "503 Service Unavailable",
    **DISPATCH_INGRESS_ERROR_STATUS,
}
logger = logging.getLogger("runtime_bridge_cloud")
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)
logger.propagate = False


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def health_document():
    """Revision provenance for /health, so a request can always be traced to
    the exact service/revision/build that handled it -- never guessed from
    deploy-log timing after the fact.

    service/revision/configuration come from Cloud Run's own runtime env
    (K_SERVICE/K_REVISION/K_CONFIGURATION -- set automatically by the
    platform, not something this app or its deploy step configures).
    git_sha has no Cloud Run-native equivalent; it is only populated when a
    deploy step explicitly sets ADM_GIT_SHA (e.g. `--set-env-vars
    ADM_GIT_SHA=$(git rev-parse HEAD)`), which nothing currently does. None
    of these are secrets -- they are already visible in the Cloud Run
    console/logs and git history -- so it is safe for this to stay on the
    public, unauthenticated /health path. Any field whose env var is unset
    or empty is null; that never affects the 200 status.
    """
    return {
        "status": "ok",
        "contract_version": CONTRACT_VERSION,
        "timestamp": iso_now(),
        "service": os.environ.get("K_SERVICE") or None,
        "revision": os.environ.get("K_REVISION") or None,
        "configuration": os.environ.get("K_CONFIGURATION") or None,
        "git_sha": os.environ.get("ADM_GIT_SHA") or None,
    }


def default_service_factory():
    import google.auth
    from googleapiclient.discovery import build
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def default_write_service_factory():
    from googleapiclient.discovery import build
    credentials, source = user_oauth_write_credentials()
    logger.info(json.dumps({"event": "drive_write_credential", "source": source}, separators=(",", ":")))
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def default_lock_registry_factory(project_id, request_id):
    return dispatch_request_registry(os.environ.get("ADM_LOCK_GCS_BUCKET"), project_id, request_id)


def json_response(start_response, status, document, request_id=None):
    raw = json.dumps(redact(document), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(raw)))]
    if request_id:
        headers.append(("X-Request-Id", request_id))
    start_response(status, headers)
    return [raw]


def error(code, message, request_id):
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def create_app(service_factory=default_service_factory, bridge_func=runtime_bridge,
                write_service_factory=default_write_service_factory,
                lock_registry_factory=default_lock_registry_factory,
                dispatch_ingress_func=handle_dispatch,
                global_invoke_func=global_invoke):
    def app(environ, start_response):
        started = time.perf_counter()
        request_id = environ.get("HTTP_X_REQUEST_ID") or uuid.uuid4().hex
        method, path = environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "/")
        status, category, project_id = "200 OK", None, None
        try:
            if method == "GET" and path == "/health":
                return json_response(start_response, status, health_document(), request_id)
            if method != "POST" or path not in ("/dispatch", DISPATCH_INGRESS_PATH, GLOBAL_INVOKE_PATH):
                status, category = "404 Not Found", "not_found"
                return json_response(start_response, status, error(category, "endpoint not found", request_id), request_id)
            expected = os.environ.get("ADM_API_KEY")
            supplied = environ.get("HTTP_AUTHORIZATION", "")
            if not expected:
                status, category = "503 Service Unavailable", "service_unconfigured"
                return json_response(start_response, status, error(category, "service authentication is not configured", request_id), request_id)
            if not supplied.startswith("Bearer ") or not hmac.compare_digest(supplied[7:], expected):
                status, category = "401 Unauthorized", "auth_failure"
                return json_response(start_response, status, error(category, "invalid bearer credential", request_id), request_id)
            if path in (DISPATCH_INGRESS_PATH, GLOBAL_INVOKE_PATH):
                try:
                    length = int(environ.get("CONTENT_LENGTH") or 0)
                    payload = json.loads(environ["wsgi.input"].read(length).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    status, category = "400 Bad Request", "malformed_request"
                    return json_response(start_response, status, error(category, "request body must be valid JSON", request_id), request_id)
                project_id = payload.get("project_id") if isinstance(payload, dict) else None
                try:
                    write_service = write_service_factory()
                except Exception:
                    status, category = "503 Service Unavailable", "drive_unavailable"
                    return json_response(start_response, status, error(category, "runtime data is unavailable", request_id), request_id)
                if path == GLOBAL_INVOKE_PATH:
                    project_id = payload.get("project") if isinstance(payload, dict) else None
                    try:
                        result = global_invoke_func(DriveRecords(write_service), write_service, lock_registry_factory, payload)
                    except GlobalInvokeError as exc:
                        status, category = GLOBAL_INVOKE_ERROR_STATUS.get(exc.code, "422 Unprocessable Entity"), exc.code
                        return json_response(start_response, status, error(exc.code, str(exc), request_id), request_id)
                    except DispatchIngressError as exc:
                        status, category = DISPATCH_INGRESS_ERROR_STATUS.get(exc.code, "422 Unprocessable Entity"), exc.code
                        return json_response(start_response, status, error(exc.code, str(exc), request_id), request_id)
                    except TaskError as exc:
                        status, category = "422 Unprocessable Entity", "global_invoke_error"
                        return json_response(start_response, status, error(category, str(exc), request_id), request_id)
                    except Exception:
                        status, category = "500 Internal Server Error", "global_invoke_exception"
                        return json_response(start_response, status, error(category, "global invoke failed", request_id), request_id)
                    return json_response(start_response, status, result, request_id)
                try:
                    result = dispatch_ingress_func(DriveRecords(write_service), write_service, lock_registry_factory, payload)
                except DispatchIngressError as exc:
                    status, category = DISPATCH_INGRESS_ERROR_STATUS.get(exc.code, "422 Unprocessable Entity"), exc.code
                    return json_response(start_response, status, error(exc.code, str(exc), request_id), request_id)
                except TaskError as exc:
                    status, category = "422 Unprocessable Entity", "dispatch_ingress_error"
                    return json_response(start_response, status, error(category, str(exc), request_id), request_id)
                except Exception:
                    status, category = "500 Internal Server Error", "dispatch_ingress_exception"
                    return json_response(start_response, status, error(category, "dispatch ingress failed", request_id), request_id)
                return json_response(start_response, status, result, request_id)
            try:
                length = int(environ.get("CONTENT_LENGTH") or 0)
                payload = json.loads(environ["wsgi.input"].read(length).decode("utf-8"))
                if not isinstance(payload, dict) or set(payload) - ALLOWED_INPUTS:
                    raise ValueError("request must be an object containing supported fields only")
                if not isinstance(payload.get("user_request"), str) or not payload["user_request"].strip():
                    raise ValueError("user_request is required")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                status, category = "400 Bad Request", "malformed_request"
                return json_response(start_response, status, error(category, str(exc), request_id), request_id)
            project_id = payload.get("project_id")
            try:
                service = service_factory()
            except Exception:
                status, category = "503 Service Unavailable", "drive_unavailable"
                return json_response(start_response, status, error(category, "runtime data is unavailable", request_id), request_id)
            try:
                result = bridge_func(DriveRecords(service), service, payload, read_only=True)
            except TaskError as exc:
                code = "project_not_found" if "project resolution" in str(exc) else "runtime_bridge_error"
                status, category = ("404 Not Found" if code == "project_not_found" else "422 Unprocessable Entity"), code
                return json_response(start_response, status, error(code, str(exc), request_id), request_id)
            except Exception:
                status, category = "500 Internal Server Error", "runtime_bridge_exception"
                return json_response(start_response, status, error(category, "runtime bridge failed", request_id), request_id)
            if result.get("contract_version") != CONTRACT_VERSION:
                status, category = "500 Internal Server Error", "contract_mismatch"
                return json_response(start_response, status, error(category, "runtime bridge contract mismatch", request_id), request_id)
            return json_response(start_response, status, result, request_id)
        finally:
            logger.info(json.dumps({"request_id": request_id, "timestamp": iso_now(), "status": status.split()[0], "latency_ms": round((time.perf_counter()-started)*1000), "project_id": project_id, "error_category": category}, separators=(",", ":")))
    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    with make_server("0.0.0.0", port, app) as server:
        server.serve_forever()
