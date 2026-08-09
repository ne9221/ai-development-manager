# AI Development Rules

version: 0.1.1
last_updated: 2026-08-09

Single source of truth for cross-project AI-development rules. This document
governs how AI coding tools (ChatGPT, Claude Code, Codex, Antigravity,
Gemini, ...) are coordinated across projects. It does not replace any
individual project's own `TASKS` or business rules - it sits above them.

## Rules

1. Cloud-first. The local machine is only a necessary execution environment
   (collectors, cache, login/session state, scratch/temp) - never the system
   of record.
2. Before dispatching a task to an AI, read the latest quota status for the
   candidate AIs first.
3. Choose an AI based on task nature + current quota + reset time - not a
   hardcoded quota percentage threshold.
4. Prefer spreading independent work across different AIs' separate quotas
   in parallel, where task nature allows it.
5. A single AI task should default to roughly 20 minutes; if it's expected
   to run longer, split it.
6. Every task must leave behind progress / a handoff, so the next AI or a
   new conversation doesn't need to re-read the whole project and chat
   history.
7. Going forward, tasks should record actual time spent and quota/token
   consumed, to improve future estimates.
8. This document is the top-level cross-project standard; it does not
   override any individual project's own `TASKS` or business rules.
9. Quota data must carry `source`, `confidence`, and `last_updated`. Stale
   data must not be treated as a live signal for dispatch decisions.
10. Never fabricate a fixed usage percentage for a provider just because it
    currently lacks an automatic quota API (e.g. Antigravity, Gemini App in
    v0.1) - use the manual `status` enum instead.
11. For code changes, after project business rules, acceptance criteria,
    required tests, stability, and regression protection are satisfied, prefer
    Ponytail/minimal-change and YAGNI: reuse existing code, avoid unrelated
    refactors or unnecessary abstractions/wrappers/dependencies, and modify only
    the task's required scope. Minimality must never reduce correctness.

## Changelog

- 0.1.0 (2026-08-09): Initial version. Rules 1-10 above, derived from the
  Phase-0 quota-source PoCs for Codex, Claude Code, Antigravity, and Gemini
  App / Google AI Pro.
- 0.1.1 (2026-08-09): Added rule 11 for subordinate Ponytail/minimal-change
  execution policy on code modifications.
