"""Web dashboard for real-time optimizer state monitoring."""

from .server import start_dashboard, DashboardStateTracer

__all__ = ["start_dashboard", "DashboardStateTracer"]
