# AI Development Manager

Independent, cross-project management layer for coordinating multiple AI
coding tools (ChatGPT, Claude Code, Codex, Antigravity, Gemini, ...).

This repo is **not** a runtime dependency of any business project. If this
repo/layer disappears, every business project must remain independently
developable.

## What lives here

- `AI-DEVELOPMENT-RULES.md` - the cross-project rules SSOT (versioned).
- `schema/status.schema.json` - provider-neutral quota/usage status schema
  (v0.1), plus `schema/status.example.json` showing real PoC-derived values
  for Codex / Claude Code / Antigravity / Gemini App.
- `collectors/` - not yet implemented; see `collectors/README.md` for the
  planned per-provider sources.
- `docs/QUOTA-SOURCES.md` - PoC findings on where each provider's quota data
  can (or cannot) be read from.

## What lives on Google Drive instead (not in this repo)

Runtime state - `PROJECTS.md`, `AI-RESOURCE-STATUS/status.json`, `TASKS/`,
`HANDOFFS/`, `TASK-HISTORY/`, `CHANGELOG.md` - is the Drive folder's
responsibility, not this repo's. This repo holds code, schema, and version
history; Drive holds the live/mutable data. See `docs/DRIVE-STRUCTURE.md`
for the exact folder layout to create.

## Status

v0.1 - skeleton only. No collectors, no scheduler, no Drive/Sheets sync, no
task/handoff workflow yet.
