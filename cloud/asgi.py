"""Single-service ASGI composition for REST and MCP endpoints."""

import hmac
import os
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware.wsgi import WSGIMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount
from mcp.server.transport_security import TransportSecuritySettings

from cloud.app import app as rest_app
from manager.mcp_adapter import server


class MCPBearerAuth:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            expected = os.environ.get("ADM_API_KEY")
            headers = dict(scope.get("headers", []))
            supplied = headers.get(b"authorization", b"").decode("utf-8", "replace")
            status = 503 if not expected else 401
            if not expected or not supplied.startswith("Bearer ") or not hmac.compare_digest(supplied[7:], expected):
                await JSONResponse({"error": {"code": "service_unconfigured" if not expected else "auth_failure"}}, status_code=status)(scope, receive, send)
                return
        await self.app(scope, receive, send)


mcp_host = os.environ.get("MCP_ALLOWED_HOST", "localhost")
mcp_app = server.streamable_http_app(
    streamable_http_path="/", json_response=True, stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[mcp_host],
        allowed_origins=[f"https://{mcp_host}"],
    ),
)


@asynccontextmanager
async def lifespan(_app):
    async with server.session_manager.run():
        yield


def create_asgi(rest=rest_app, mcp=mcp_app):
    return Starlette(
        routes=[Mount("/mcp", app=MCPBearerAuth(mcp)), Mount("/", app=WSGIMiddleware(rest))],
        lifespan=lifespan,
    )


app = create_asgi()
