"""
Convenient entry‑point for running the Bugzzy FastAPI backend on Windows.

This script starts uvicorn with a SelectorEventLoop to avoid
psycopg's incompatibility with ProactorEventLoop on Windows.
"""

import asyncio
import selectors
from uvicorn import Config, Server

def main() -> None:
    config = Config(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        log_config="log_config.json",
        loop="asyncio",
    )
    server = Server(config)
    # Run the server using a SelectorEventLoop
    asyncio.run(
        server.serve(),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )

if __name__ == "__main__":
    main()
