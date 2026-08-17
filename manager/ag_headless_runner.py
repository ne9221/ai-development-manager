"""Headless Antigravity fallback runner with strict Fail-Closed Auth Guard.

This module provides AgHeadlessRunner, which inherits from OfficialAgCliRunner
with default_mode="headless", ensuring full backward compatibility and strict Fail-Closed Auth.
"""

from __future__ import annotations

from typing import Any, Callable

from manager.ag_cli_runner import (
    AgCliProcess as AgHeadlessProcess,
    OfficialAgCliRunner,
    resolve_ag_cli_executable,
    resolve_canonical_gemini_home,
    sanitize_ag_environment,
    verify_auth_identity,
)


class AgHeadlessRunner(OfficialAgCliRunner):
    """Headless Antigravity runner subclass defaulting to mode='headless'."""

    def __init__(
        self,
        executable_resolver: Callable[..., Any] | None = None,
        auth_verifier: Callable[[], str] | None = None,
    ):
        super().__init__(
            executable_resolver=executable_resolver,
            auth_verifier=auth_verifier,
            default_mode="headless",
        )


def resolve_ag_executable(explicit: str | None = None) -> str:
    path, _ = resolve_ag_cli_executable(explicit)
    return path


__all__ = [
    "AgHeadlessProcess",
    "AgHeadlessRunner",
    "resolve_ag_executable",
    "resolve_canonical_gemini_home",
    "sanitize_ag_environment",
    "verify_auth_identity",
]
