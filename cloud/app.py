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

from manager.runtime_bridge import redact, runtime_bridge
from manager.tasks import DriveRecords, TaskError


CONTRACT_VERSION = "1.0"
ALLOWED_INPUTS = {"project_id", "user_request", "task_id", "task_type", "complexity", "preferred_provider", "excluded_provider", "multi_task"}
logger = logging.getLogger("runtime_bridge_cloud")
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_service_factory():
    import google.auth
    from googleapiclient.discovery import build
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def json_response(start_response, status, document, request_id=None):
    raw = json.dumps(redact(document), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(raw)))]
    if request_id:
        headers.append(("X-Request-Id", request_id))
    start_response(status, headers)
    return [raw]


def error(code, message, request_id):
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def create_app(service_factory=default_service_factory, bridge_func=runtime_bridge):
    def app(environ, start_response):
        started = time.perf_counter()
        request_id = environ.get("HTTP_X_REQUEST_ID") or uuid.uuid4().hex
        method, path = environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "/")
        status, category, project_id = "200 OK", None, None
        try:
            if method == "GET" and path == "/health":
                return json_response(start_response, status, {"status": "ok", "contract_version": CONTRACT_VERSION, "timestamp": iso_now()}, request_id)
            if method != "POST" or path != "/dispatch":
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
