"""Verification Loop / Acceptance Layer -- Phase B-2 verification controller.

Deterministic derivation over fixture data, plus the append-only ticket and
report stores that a real controller would sit on. No real provider, Excel,
Drive, GitHub, screenshot or Session Center integration lives here; those
surfaces are fixture fields so the admission and classification logic can be
proven before any real checker exists to be trusted.

Layout, roughly outside-in:

  paths           canonical repo-relative paths (rules must not be dodgeable
                  by respelling a path)
  identity        the (provider, account_id, session_id) triple and its
                  Session Center resolution
  models          Task / Execution / Bundle / Ticket / Report fixtures
  bundle          frozen-bundle hashing and contract validation
  tickets         ticket ids derived from the round tuple they bind
  risk            effective_risk = max(declared, diff, component)
  impact          which gates a diff invalidates (cost only, never the final
                  requirement)
  budget          retry / repair / review caps folded over the Execution chain
  classification  which failure class the evidence actually earns
  admission       full lineage cross-binding for one report
  evaluator       the guard order, and the single next action
  aggregator      Task close eligibility over a single deliverable SHA
  stores          create-only ticket/report ledgers, refusing production roots

Two invariants hold across all of it. **ACCEPTED is never stored** -- there is
no field for it, so contract revision automatically invalidates old conclusions
and contradictory evidence automatically overturns them. And **the loop cannot
express a production PASS** -- a production-risk execution or a
production-write gate can only ever route to the Production Fix Protocol.

Deliberately namespaced apart from manager.acceptance_gate, a different,
pre-existing mechanism (CONTROLLED_ACCEPTANCE_GATE is a provider-unavailable
reroute and has nothing to do with this).

Design record: docs/verification-loop/PHASE-A-V3-ARCHITECTURE.md.
"""
