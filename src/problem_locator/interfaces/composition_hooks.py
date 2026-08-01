"""Thin S08-owned dependency injection seam for the S06 adapters."""

from __future__ import annotations

from dataclasses import dataclass

from problem_locator.contracts.ports import (
    ApplicationCommandPort,
    ApplicationQueryPort,
    StateAdminPort,
)


@dataclass(frozen=True, slots=True)
class InterfaceDependencies:
    command_port: ApplicationCommandPort
    query_port: ApplicationQueryPort
    state_admin: StateAdminPort
    public_base_url: str


def create_asgi_app(dependencies: InterfaceDependencies):
    """Create the shared MCP/HTTP ASGI application after S08 injects ports."""

    from .http_app import create_http_app

    return create_http_app(
        command_port=dependencies.command_port,
        query_port=dependencies.query_port,
        state_admin=dependencies.state_admin,
        public_base_url=dependencies.public_base_url,
    )


__all__ = ["InterfaceDependencies", "create_asgi_app"]
