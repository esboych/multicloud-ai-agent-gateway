from __future__ import annotations

import uvicorn

from multicloud_ai_agent_gateway.app import build_app
from multicloud_ai_agent_gateway.settings import load_settings


def main() -> None:
    settings = load_settings()
    app = build_app(settings)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.health_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
