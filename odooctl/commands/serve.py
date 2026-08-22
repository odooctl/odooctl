"""odooctl serve — start the local API server with optional static SPA.

FastAPI and uvicorn must be installed (``pip install odooctl[api]``).
The server binds to 127.0.0.1 by default (localhost-only).

By default, ``odooctl serve`` automatically serves the packaged SPA from
``odooctl/web/dist/`` at ``/``. Pass ``--static-dir`` to override with a
custom directory (useful during SPA development). The API routes under
``/projects`` and ``/operations`` always take priority over static files.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


_ALLOWED_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def validate_allowed_hosts(hosts: list[str]) -> list[str]:
    """Validate explicit reverse-proxy host names without accepting wildcards."""
    validated: list[str] = []
    for host in hosts:
        if not _ALLOWED_HOST_RE.fullmatch(host) or ".." in host or "*" in host:
            raise ValueError(
                f"Invalid allowed host {host!r}; use an exact hostname or IP address, never a wildcard."
            )
        validated.append(host)
    return validated


def _bundled_dist() -> Path:
    """Return the path to the packaged SPA dist directory bundled with odooctl."""
    return Path(__file__).parent.parent / "web" / "dist"


def run(
    host: str = "127.0.0.1",
    port: int = 8787,
    api_key: str | None = None,
    static_dir: Path | None = None,
    reload: bool = False,
    allowed_hosts: list[str] | None = None,
) -> None:
    try:
        import uvicorn  # noqa: F401
        from fastapi import FastAPI  # noqa: F401
    except ImportError:
        raise SystemExit(
            "FastAPI and uvicorn are required for 'odooctl serve'.\n"
            "Install the optional extras: pip install odooctl[api]"
        )

    if api_key is None:
        api_key = os.environ.get("ODOOCTL_API_KEY", "")
    if not api_key:
        raise SystemExit(
            "API key is required. Set --api-key or ODOOCTL_API_KEY env var."
        )

    # Auto-detect bundled SPA when no explicit --static-dir is given.
    if static_dir is None:
        bundled = _bundled_dist()
        if bundled.exists():
            static_dir = bundled

    from odooctl.api.app import create_app

    try:
        extra_allowed_hosts = validate_allowed_hosts(allowed_hosts or [])
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    app = create_app(
        api_key=api_key,
        static_dir=static_dir,
        extra_allowed_hosts=extra_allowed_hosts,
    )
    uvicorn.run(app, host=host, port=port, reload=reload)
