from __future__ import annotations

import sys

import uvicorn

from .api import create_app
from .config import load_config


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "self-knowledge":
        from .self_knowledge.__main__ import run as run_self_knowledge

        raise SystemExit(run_self_knowledge(sys.argv[2:]))

    config = load_config()
    app = create_app(config)
    uvicorn.run(app, host=config.app.host, port=config.app.port)


if __name__ == "__main__":
    main()
