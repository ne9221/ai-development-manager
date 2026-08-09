import asyncio
import json
import os
import unittest
from unittest.mock import patch

from cloud.app import create_app
from cloud.asgi import MCPBearerAuth, create_asgi


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


class ASGIAuthTests(unittest.TestCase):
    def test_mcp_authentication_boundary(self):
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
