"""Web dashboard server for real-time optimizer state monitoring.

Provides an HTTP server with WebSocket support that broadcasts
OptimizerState snapshots to connected browser clients.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp import web

from .serializer import serialize_state

logger = logging.getLogger(__name__)


class DashboardStateTracer:
    """StateTracer that pushes snapshots to a WebSocket broadcast queue.

    Extends the base StateTracer with an asyncio.Queue for dashboard updates.
    Designed to be used as a drop-in replacement for StateTracer.
    """

    def __init__(self):
        # Import here to avoid circular imports
        from optimizer.tracing import StateTracer
        self._base = StateTracer()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10)

    @property
    def transitions(self):
        return self._base.transitions

    @property
    def queue(self) -> asyncio.Queue:
        return self._queue

    def on_enter(self, node_name: str, state) -> None:
        self._base.on_enter(node_name, state)

    def on_exit(self, node_name: str, state) -> None:
        self._base.on_exit(node_name, state)
        self._push_snapshot(state)

    def on_edge(self, from_node: str, to_node: str, edge_type: str = "static") -> None:
        self._base.on_edge(from_node, to_node, edge_type)

    def export(self, path: str) -> None:
        self._base.export(path)

    def _push_snapshot(self, state) -> None:
        """Serialize state and push to queue. Drop oldest if full."""
        try:
            snapshot = serialize_state(state)
            snapshot["transitions"] = list(self._base.transitions)
            try:
                self._queue.put_nowait(snapshot)
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self._queue.put_nowait(snapshot)
        except Exception as e:
            logger.debug(f"[dashboard] Snapshot serialization failed: {e}")

    def push_tool_event(self, state) -> None:
        """Push a lightweight update containing only tool trace data."""
        try:
            import dataclasses
            from .serializer import _make_json_safe
            snapshot = {
                "type": "tool_trace_update",
                "tool_call_trace": _make_json_safe(dataclasses.asdict(state.context))["tool_call_trace"],
                "iteration": {"current": state.iteration.current, "tool_round": state.iteration.tool_round},
                "timing": {"latest_wns": state.timing.latest_wns, "best_wns": state.timing.best_wns},
            }
            try:
                self._queue.put_nowait(snapshot)
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self._queue.put_nowait(snapshot)
        except Exception as e:
            logger.debug(f"[dashboard] Tool event push failed: {e}")


async def _websocket_handler(request: web.Request) -> web.WebSocketResponse:
    """WebSocket handler: send current state, then stream updates."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    tracer: DashboardStateTracer = request.app["tracer"]
    state = request.app["state"]

    # Send current state immediately
    try:
        snapshot = serialize_state(state)
        snapshot["transitions"] = list(tracer.transitions)
        await ws.send_json(snapshot)
    except Exception as e:
        logger.debug(f"[dashboard] Initial snapshot send failed: {e}")

    # Stream updates from queue
    try:
        while not ws.closed:
            try:
                snapshot = await asyncio.wait_for(tracer.queue.get(), timeout=5.0)
                if not ws.closed:
                    await ws.send_json(snapshot)
            except asyncio.TimeoutError:
                # Send heartbeat to detect stale connections
                if not ws.closed:
                    await ws.ping()
            except asyncio.CancelledError:
                break
    except Exception as e:
        logger.debug(f"[dashboard] WebSocket error: {e}")
    finally:
        if not ws.closed:
            await ws.close()

    return ws


async def _index_handler(request: web.Request) -> web.Response:
    """Serve the self-contained HTML dashboard."""
    html_path = Path(__file__).parent / "static" / "index.html"
    return web.FileResponse(html_path)


def create_app(state, tracer: DashboardStateTracer) -> web.Application:
    """Create aiohttp application with routes."""
    app = web.Application()
    app["state"] = state
    app["tracer"] = tracer
    app.router.add_get("/", _index_handler)
    app.router.add_get("/ws", _websocket_handler)
    return app


async def start_dashboard(
    state,
    tracer: DashboardStateTracer,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> web.AppRunner:
    """Start dashboard HTTP server as a background task.

    Returns AppRunner — caller must call ``await runner.cleanup()`` on exit.
    """
    app = create_app(state, tracer)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"[dashboard] Dashboard running at http://{host}:{port}")
    return runner
