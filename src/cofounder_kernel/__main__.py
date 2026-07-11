from __future__ import annotations

import uvicorn

from .api import create_app
from .config import load_config


def main() -> None:
    config = load_config()
    app = create_app(config)
    uvicorn.run(app, host=config.app.host, port=config.app.port)


if __name__ == "__main__":
    main()

