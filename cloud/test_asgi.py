import asyncio
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from cloud.app import create_app
from cloud.asgi import MCPBearerAuth, app as asgi_app, create_asgi, server


async def request(app, authorization=None, path="/mcp/", method="POST", body=b""):
    sent = []
    headers = [] if authorization is None else [(b"authorization", authorization.encode())]
    if body:
        headers += [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http", "server": ("test", 80), "client": ("test", 1), "method": method, "path": path, "raw_path": path.encode(), "query_string": b"", "headers": headers}
    async def receive(): return {"type": "http.request", "body": body, "more_body": False}
    async def send(message): sent.append(message)
    await app(scope, receive, send)
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    content = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return status, content


async def mcp_quota_request(authorization=None):
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "adm_runtime_quota_status", "arguments": {}},
    }).encode()
    sent, received = [], False
    headers = [
        (b"host", b"localhost"), (b"content-type", b"application/json"),
        (b"accept", b"application/json, text/event-stream"),
    ]
    if authorization:
        headers.append((b"authorization", authorization.encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "https",
        "server": ("localhost", 443), "client": ("test", 1), "method": "POST", "path": "/mcp/",
        "raw_path": b"/mcp/", "root_path": "", "query_string": b"", "headers": headers,
    }
    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}
    async def send(message): sent.append(message)
    await asgi_app(scope, receive, send)
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    content = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return status, content


class ASGIAuthTests(unittest.TestCase):
    def test_mcp_authentication_boundary(self):
        self.assertTrue(server.session_manager.security_settings.enable_dns_rebinding_protection)
        self.assertEqual(["localhost"], server.session_manager.security_settings.allowed_hosts)
        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        app = MCPBearerAuth(downstream)
        with patch.dict(os.environ, {"ADM_API_KEY": "secret"}):
            self.assertEqual(401, asyncio.run(request(app))[0])
            self.assertEqual(401, asyncio.run(request(app, "Bearer wrong"))[0])
            self.assertEqual(204, asyncio.run(request(app, "Bearer secret"))[0])
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(503, asyncio.run(request(app))[0])

    def test_runtime_quota_tool_through_authenticated_mcp_transport(self):
        now = datetime.now(timezone.utc).isoformat()
        raw = {
            "schema_version": "0.1.0", "generated_at": now, "raw": "backend-secret",
            "providers": [{
                "provider": "codex", "source": "codex_app_server", "source_type": "official",
                "confidence": "official", "status": "ok", "last_updated": now,
                "windows": [{"name": "primary", "used_percent": 20, "remaining_percent": 80}],
                "metadata": {"token": "backend-secret"},
            }],
        }
        async def check():
            with patch.dict(os.environ, {"ADM_API_KEY": "secret"}), \
                 patch("manager.runtime_bridge.read_drive_status", return_value=raw):
                async with server.session_manager.run():
                    missing = await mcp_quota_request()
                    allowed = await mcp_quota_request("Bearer secret")
            return missing, allowed
        missing, allowed = asyncio.run(check())
        self.assertEqual(401, missing[0]); self.assertEqual(200, allowed[0])
        contract = json.loads(allowed[1])["result"]["structuredContent"]
        self.assertEqual("1.0", contract["contract_version"])
        self.assertEqual(80, contract["providers"]["codex"]["windows"][0]["remaining_percent"])
        self.assertNotIn("backend-secret", json.dumps(contract))

    def test_existing_rest_routes_survive_asgi_composition(self):
        class Service:
            def files(self): return object()
        bridge = lambda *args, **kwargs: {"contract_version": "1.0"}
        app = create_asgi(rest=create_app(lambda: Service(), bridge))
        health_status, health = asyncio.run(request(app, path="/health", method="GET"))
        self.assertEqual(200, health_status); self.assertEqual("1.0", json.loads(health)["contract_version"])
        payload = json.dumps({"user_request": "work"}).encode()
        with patch.dict(os.environ, {"ADM_API_KEY": "secret"}):
            dispatch_status, dispatch = asyncio.run(request(app, "Bearer secret", "/dispatch", body=payload))
        self.assertEqual(200, dispatch_status); self.assertEqual("1.0", json.loads(dispatch)["contract_version"])


if __name__ == "__main__": unittest.main()
