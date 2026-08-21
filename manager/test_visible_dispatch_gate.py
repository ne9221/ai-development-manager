"""Pytest/unittest entry point for the visible-dispatch acceptance harness."""

from tools.acceptance.visible_dispatch import run


def test_visible_dispatch_acceptance():
    assert run()
