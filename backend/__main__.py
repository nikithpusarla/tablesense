"""Start TableSense: ``python -m backend`` (host/port from config.yaml; PORT env var overrides)."""
from __future__ import annotations

import os

import uvicorn

from .config import load_config


def main() -> None:
    cfg = load_config()
    host = str(cfg.get_path("server.host", "0.0.0.0"))
    port = int(os.getenv("PORT") or cfg.get_path("server.port", 8000))
    uvicorn.run("backend.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
