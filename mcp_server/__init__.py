"""MCP server exposing CapitalView's data to agent clients."""

from .server import build_mcp_route, build_mcp_server

__all__ = ["build_mcp_route", "build_mcp_server"]
