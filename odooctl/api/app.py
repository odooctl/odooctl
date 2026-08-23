"""FastAPI application factory for the odooctl local API.

Create the app with ``create_app(api_key=..., registry_loader=...)`` and
hand it to uvicorn. By default the app binds to localhost-only via
``TrustedHostMiddleware``.

Optional static SPA: pass ``static_dir`` pointing to a pre-built SPA dist
directory and it will be mounted at ``/`` (served as a fallback after API
routes). The SPA fallback ``index.html`` is read once at app creation and
served from memory for the lifetime of the process; ``odooctl serve`` is a
long-running process, so after rebuilding the SPA dist, restart the server
to pick up a new ``index.html``.

No privileged imports — satisfies the runner contract.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from odooctl.api.routes_auth import router as auth_router
from odooctl.api.routes_operations import router as operations_router
from odooctl.api.routes_projects import router as projects_router
from odooctl.security import tokens


def create_app(
    api_key: str,
    *,
    registry_loader: Callable | None = None,
    allowed_hosts: list[str] | None = None,
    extra_allowed_hosts: list[str] | None = None,
    static_dir: Path | None = None,
    auth_dir: Path | None = None,
) -> FastAPI:
    """Create and configure the odooctl FastAPI application.

    :param api_key: Shared HMAC key used to verify bearer tokens.
    :param registry_loader: Callable returning a ``Registry``; defaults to
        ``odooctl.registry.load_registry`` so tests can inject a fake.
    :param allowed_hosts: Hosts allowed by ``TrustedHostMiddleware``; defaults
        to ``["127.0.0.1", "localhost"]`` for localhost-only operation.
    :param extra_allowed_hosts: Additional hosts appended to the default set
        (e.g. tests pass ``["testclient"]``); ignored when ``allowed_hosts`` is
        given explicitly.
    :param static_dir: Optional path to a pre-built SPA dist directory mounted
        at ``/`` after all API routes.
    :param auth_dir: Directory holding the server-level user/session stores;
        defaults to the registry's directory (``~/.config/odooctl``).
    """
    # Key-strength floor (F24): a short HMAC key makes bearer/capability tokens
    # brute-forceable, so it is rejected unconditionally regardless of how the
    # key reached us. This is the primary defense; ``get_principal`` re-checks
    # as a backstop.
    tokens.enforce_key_strength(api_key)

    if registry_loader is None:
        from odooctl.registry import load_registry

        registry_loader = load_registry

    app = FastAPI(
        title="odooctl API",
        description="Local management API for self-hosted Odoo deployments.",
        version="1.0.0",
    )

    app.state.api_key = api_key
    app.state.registry_loader = registry_loader
    app.state.auth_dir = Path(auth_dir) if auth_dir is not None else None

    if allowed_hosts is None:
        # Localhost-only by default. "testclient" (the Starlette TestClient
        # default Host) is deliberately NOT in the production default; tests
        # opt in via extra_allowed_hosts.
        allowed_hosts = ["127.0.0.1", "localhost"]
        if extra_allowed_hosts:
            allowed_hosts = [*allowed_hosts, *extra_allowed_hosts]

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    app.include_router(auth_router)
    app.include_router(projects_router)
    app.include_router(operations_router)

    if static_dir is not None and Path(static_dir).exists():
        from fastapi.responses import FileResponse, HTMLResponse

        _static = Path(static_dir).resolve()
        # Cache the SPA fallback index.html bytes at startup instead of
        # re-reading the file on every request. Rebuilding the SPA dist
        # requires a server restart to pick up a new index.html.
        _index_bytes = (_static / "index.html").read_bytes()

        @app.get("/{full_path:path}", include_in_schema=False)
        async def _spa(full_path: str):
            candidate = (_static / full_path).resolve()
            try:
                candidate.relative_to(_static)
            except ValueError:
                return HTMLResponse(_index_bytes)
            if candidate.is_file():
                return FileResponse(str(candidate))
            return HTMLResponse(_index_bytes)

    return app
