from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "self-knowledge":
        from .self_knowledge.__main__ import run as run_self_knowledge

        raise SystemExit(run_self_knowledge(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        # Governed external-agent surface over stdio. Off by default: it runs only
        # when invoked explicitly here, never from autostart/tray. See
        # ZADE-MCP-SURFACE.md and mcp_server.py.
        from .mcp_server import run as run_mcp

        raise SystemExit(run_mcp(sys.argv[2:]))

    import uvicorn

    from .api import create_app
    from .config import load_config

    config = load_config()
    app = create_app(config)
    uvicorn.run(app, host=config.app.host, port=config.app.port)


if __name__ == "__main__":
    main()
