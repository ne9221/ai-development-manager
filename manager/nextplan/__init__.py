"""NextPlan foundation: understand what an agent did, whether it is true, what
failed, and what to do next.

Read-only and fixture-backed. Nothing in this package dispatches work, writes
a repository, writes Drive or changes an existing lifecycle record; nothing in
production imports it yet.

  vocabulary   closed sets of actions, states, roles and evidence levels
  atlas        the Failure Atlas (failure_atlas.json) and its validator
  result       the canonical AI result contract and its evidence levels
  extract      agent output -> REPORTED facts, source tier recorded
  verify       probes that upgrade REPORTED to VERIFIED or CONTRADICTED
  classify     verified result -> Failure Atlas codes
  planner      deterministic NextPlan decision
  pipeline     extract -> verify -> classify -> plan, read-only

Design record: docs/nextplan/EXECUTION-PLAN-V1.md. Deliberately separate from
the unmerged manager.verification_loop (acceptance of a candidate SHA from
checker reports); when that merges, its derived acceptance state becomes one
more planner input.
"""
