"""Standalone loopback HTTP MCP; never imports the task runtime or database."""

from contextlib import asynccontextmanager
import secrets
import ssl
import os
from pathlib import Path

from starlette.responses import JSONResponse

from desktop.files import private_write
from control_api.mcp_launcher import local_url, startup_lock


def token_path(settings):
    return settings.data_dir.resolve() / "mcp-token"


def local_token(settings):
    path = token_path(settings)
    with startup_lock(path.parent / ".mcp-token.lock"):
        if not path.exists():
            private_write(path, secrets.token_urlsafe(32))
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise ValueError("Invalid local MCP token; restore or regenerate the token file")
        return token


class LocalBoundary:
    """Guard all local API/Console routes; authenticate MCP and setup requests."""

    def __init__(self, app, *, port, scheme, token, setup_only=False):
        self.app, self.token = app, token
        self.setup_only = setup_only
        self.hosts = {f"{host}:{port}" for host in ("127.0.0.1", "localhost", "[::1]")}
        if port == (443 if scheme == "https" else 80):
            self.hosts.update({"127.0.0.1", "localhost", "[::1]"})
        self.origins = {f"{scheme}://{host}" for host in self.hosts}

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if self.setup_only and not path.startswith("/api/setup"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope["headers"]}
        counts = {name: sum(k.lower() == name for k, _ in scope["headers"])
                  for name in (b"host", b"origin", b"authorization")}
        forbidden = (any(count > 1 for count in counts.values()) or headers.get("host", "").lower() not in self.hosts or
                     ("origin" in headers and headers["origin"] not in self.origins))
        protected = path == "/mcp" or path.startswith("/mcp/") or path.startswith("/api/setup")
        authenticated = secrets.compare_digest(headers.get("authorization", "").encode("utf-8"),
                                                 ("Bearer " + self.token).encode("utf-8"))
        # Exact local Host/Origin checks above still apply. Fetch Metadata is
        # browser-controlled; cross-site pages cannot obtain a setup credential.
        bootstrap = (path == "/api/setup/access" and scope.get("method") == "GET"
                     and headers.get("sec-fetch-site") == "same-origin")
        if forbidden or (protected and not authenticated and not bootstrap):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                response = JSONResponse({"detail": "Invalid local origin/host" if forbidden else
                                         "Local credential required"}, status_code=403 if forbidden else 401)
                await response(scope, receive, send)
            return
        return await self.app(scope, receive, send)


def http_base_url(settings):
    scheme = "https" if settings.api_ssl_certfile and settings.api_ssl_keyfile else "http"
    host = "[::1]" if settings.api_host == "::1" else settings.api_host
    return f"{scheme}://{host}:{settings.mcp_http_port}"


def create_http_app(settings, *, client_factory=None):
    # Only import the optional SDK when explicitly enabled.
    from mcp.server.transport_security import TransportSecuritySettings
    from control_api.mcp import create_server
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    if settings.api_host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("HTTP MCP/setup requires a loopback API host")
    host = "[::1]" if settings.api_host == "::1" else settings.api_host
    scheme = "https" if settings.api_ssl_certfile and settings.api_ssl_keyfile else "http"
    base_url = local_url(f"{scheme}://{host}:{settings.api_port}")
    if settings.mcp_http_port == settings.api_port:
        raise ValueError("HTTP MCP needs a separate port from the task backend")
    verify = ssl.create_default_context(cafile=settings.api_ssl_certfile) if scheme == "https" else True
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{host}:{settings.mcp_http_port}" for host in ("127.0.0.1", "localhost", "[::1]")],
        allowed_origins=[f"{scheme}://{host}:{settings.mcp_http_port}" for host in ("127.0.0.1", "localhost", "[::1]")])
    server = create_server(base_url, verify=verify, client_factory=client_factory,
                           expected_backend={"data_dir": settings.data_dir.resolve(), "workspace": Path.cwd()},
                           streamable_http_path="/",
                           transport_security=security)
    mcp_app = server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with server.session_manager.run():
            yield

    async def identity(request):
        return JSONResponse({"service": "clickclick-mcp", "pid": os.getpid(),
            "data_dir": str(settings.data_dir.resolve()), "workspace": str(Path.cwd()),
            "backend_url": base_url})

    app = Starlette(routes=[Route("/mcp/identity", identity), Mount("/mcp", app=mcp_app)], lifespan=lifespan)
    app.add_middleware(LocalBoundary, port=settings.mcp_http_port, scheme=scheme, token=local_token(settings))
    app.state.mcp_server = server
    return app


def main():
    import uvicorn
    from shared.config import get_settings
    settings = get_settings()
    if bool(settings.api_ssl_certfile) != bool(settings.api_ssl_keyfile):
        raise ValueError("Both TLS certificate and key are required")
    uvicorn.run(create_http_app(settings), host=settings.api_host, port=settings.mcp_http_port,
                ssl_certfile=settings.api_ssl_certfile or None,
                ssl_keyfile=settings.api_ssl_keyfile or None, log_level="info")


if __name__ == "__main__":
    main()
