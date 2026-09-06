"""Verification Loop / Acceptance Layer -- Phase B-1.

Deterministic reference evaluator over fixture data only. No real provider,
Excel, Drive, GitHub, screenshot or Session Center integration lives here;
those surfaces are represented by fixture fields so the evaluator's admission
and classification logic can be proven independent of any real checker.

Deliberately namespaced apart from manager.acceptance_gate (a different,
pre-existing mechanism -- see that module's docstring) so this new
Task/Execution completion-acceptance concept is never confused with it.
"""
