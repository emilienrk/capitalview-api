"""MCP server exposing CapitalView's data to agent clients."""

from .server import build_mcp_app, build_mcp_server

__all__ = ["build_mcp_app", "build_mcp_server"]
