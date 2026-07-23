"""
Telemetry Proxy — main entry point.
"""
import asyncio
import logging
import sys
from typing import Optional

from aiohttp import web

from config import ProxyConfig
from reporter import TelemetryReporter
from handler import ProxyHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("proxy.main")


async def create_app(config: Optional[ProxyConfig] = None) -> web.Application:
    """Create the aiohttp application."""
    if config is None:
        config = ProxyConfig.from_env()
    reporter = TelemetryReporter(
        endpoint=config.observability_endpoint,
        timeout=config.observability_timeout,
    )
    handler = ProxyHandler(config, reporter)

    app = web.Application()
    app["handler"] = handler
    app["reporter"] = reporter

    # Routes
    app.router.add_get("/health", handler.handle_health)
    app.router.add_route("*", "/{path:.*}", handler.handle_request)

    # Lifecycle
    async def on_startup(app: web.Application):
        h = app["handler"]
        r = app["reporter"]
        await h.setup()
        await r.start()

    async def on_cleanup(app: web.Application):
        h = app["handler"]
        r = app["reporter"]
        await h.cleanup()
        await r.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app


def main():
    config = ProxyConfig.from_env()
    logger.info(
        "Starting Telemetry Proxy: listen=%s:%d, upstream=%s, observability=%s, payload=%s",
        config.listen_host, config.listen_port,
        config.upstream_url, config.observability_endpoint,
        config.payload_strategy,
    )
    app = create_app_sync(config)
    web.run_app(app, host=config.listen_host, port=config.listen_port)


def create_app_sync(config: ProxyConfig) -> web.Application:
    """Create app synchronously (delegates to async create_app)."""
    loop = asyncio.new_event_loop()
    try:
        app = loop.run_until_complete(create_app(config))
        return app
    finally:
        if not loop.is_closed():
            loop.stop()


if __name__ == "__main__":
    main()
