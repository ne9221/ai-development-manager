# AI Development Rules

version: 0.1.4
last_updated: 2026-08-26

Single source of truth for cross-project AI-development rules. This document
governs how AI coding tools (ChatGPT, Claude Code, Codex, Antigravity,
Gemini, ...) are coordinated across projects. It does not replace any
individual project's own `TASKS` or business rules - it sits above them.

## Rules

1. Cloud-first. Except software explicitly intended for shared company
   computers and required to run offline, project runtime state, reports,
   overview, task/handoff, rules, research records, and management files use
   Google Drive/Workspace, GitHub, or another approved cloud service as SSOT.
   A company computer's encrypted local files must not become SSOT merely for
   convenience. Scratch/temp/cache/login/session state may remain local. Before
   creating other persistent local project data, report why cloud is unsuitable,
   why local storage is required, its location/content, and its impact on
   cross-AI/device handoff; obtain user confirmation first.
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
12. Every AI work report must identify `AI: <Codex|Claude|Antigravity>`,
    `Project: <project>`, and `Task: <task_id>`; include `Conversation:` and
    `Run/Session:` whenever they apply.
13. Each AI development project maintains a user/ChatGPT-readable Development
    Overview with stable item IDs (for example `P01`). It summarizes pending,
    in-progress, validation, completed, deferred, and cancelled/merged work;
    it does not replace detailed TASKS, README, HANDOFF, AGENTS, or project
    rules.
14. Use a GitHub Research Gate only for a new case, a new technical/architecture
    problem, repeated failure/high cost, planned foundational tool, or likely
    mature reusable solution. Do not re-research ordinary UI tweaks, located
    bugs, small rule-consistent changes, or recently reviewed unchanged issues.
    Record reviewed projects, decision, selection/rejection reasons, and the
    recheck trigger.
15. Run weekly GitHub Discovery every Monday at 09:00 Asia/Taipei. Search actual
    needs such as AI coding/multi-agent, provider tooling, MCP/memory/
    orchestration, audit/accounting, Office/PDF/document processing, personal
    accounting, and productivity. Keep only a small set worth direct adoption,
    integration/reference, or a new project; avoid popularity-only and
    unchanged repeat findings.
16. This file is the cross-project Global Rules SSOT. Every AI task applies it
    before project-specific rules, and rule changes require version + changelog.
    Project business/acceptance requirements win within their scope; Global
    Rules govern coordination and execution. Claude, Codex, and Antigravity
    must not maintain drifting copies of these common rules.
17. When creating or substantially revising any governance file, Project
    Rules, Spec, Task Brief, Handoff, Skill, `AGENTS.md`, `CLAUDE.md`, or other
    Agent-facing instruction document, structure it using the
    `writing-for-agents` skill's principles to improve structure,
    actionability, trigger conditions, completion criteria, and context
    efficiency. `writing-for-agents` operates purely at the expression /
    agent-instruction-design layer and carries no governance authority: it
    must never delete, weaken, merge away, or reinterpret any authoritative
    governance rule, mandatory gate, SSOT, permission boundary, task
    isolation rule, security rule, research-before-build requirement,
    test/evidence requirement, commit/push requirement, status-reporting
    requirement, or other mandatory constraint. Where a simplification or
    trimming suggestion from `writing-for-agents` would conflict with an
    existing governance rule, the governance semantics and constraints must
    be preserved unchanged - only phrasing may improve. Do not force-invoke
    this skill for ordinary source code, UI/visual implementation, or
    business logic work; it applies only to authoring or restructuring
    Agent-facing instructions.

## Changelog

- 0.1.0 (2026-08-09): Initial version. Rules 1-10 above, derived from the
  Phase-0 quota-source PoCs for Codex, Claude Code, Antigravity, and Gemini
  App / Google AI Pro.
- 0.1.1 (2026-08-09): Added rule 11 for subordinate Ponytail/minimal-change
  execution policy on code modifications.
- 0.1.2 (2026-08-10): Added required cross-AI work-report identity fields.
- 0.1.3 (2026-08-10): Added Development Overview, GitHub Research Gate,
  Weekly GitHub Discovery, cloud/local exception approval, and Global Rules
  SSOT enforcement.
- 0.1.4 (2026-08-26): Added rule 17 requiring Agent-facing instruction
  documents (governance files, Project Rules, Specs, Task Briefs, Handoffs,
  Skills, `AGENTS.md`, `CLAUDE.md`, etc.) to be structured using the
  `writing-for-agents` skill's principles when created or substantially
  revised, while making explicit that the skill has no governance authority
  and must never weaken or reinterpret a mandatory rule or gate. No
  per-provider rule-mirror files (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, or
  similar) exist at the repository root as of this version; if any are added
  later they must carry the same rule 17 language, per rule 16's prohibition
  on drifting copies.
