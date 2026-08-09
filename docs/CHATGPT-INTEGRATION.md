# ChatGPT Runtime Integration Contract

`manager.runtime_bridge` is the single read/orchestration boundary for an
upper-level ChatGPT, connector, MCP adapter, local API, or plugin. It is a
Python API and clean JSON CLI today; it is not tied to any transport.

## Required client flow

For every development request, the upper-level client should:

1. Resolve or supply the project id/alias and call the runtime bridge once.
2. Use the returned Drive-backed quota freshness and recommendation instead of
   quota values from chat memory, README, or a Git snapshot.
3. Give the user the short human summary.
4. Put `generated_prompt` in a directly copyable code block.
5. Do not ask the user to repeat shared rules or manually provide quota.
6. For continuation, prefer `active_task`, `latest_handoff_summary`, and
   `next_action` over old conversation context.
7. For `multi_task`, present `execution_batches`; never auto-start providers.
8. If the bridge is unavailable, state that runtime data is unavailable and
   degrade explicitly. Never invent current quota or handoff state.

## Stable JSON boundary

Successful JSON includes `contract_version`, compact project/task/handoff
fields, request type, recommendation, estimate, quota summary/freshness,
warnings, next action, generated prompt, and optional execution batches. It
never includes raw quota documents, execution history, repo dumps, OAuth data,
tokens, or credentials.

```powershell
python -m manager.runtime_bridge --project-id ai-development-manager `
  --request "Continue Phase 9" --json
```

Project aliases are stored in the project record. Multi-task requests reuse the
existing Scheduler; single-task/new/continuation requests reuse the Dispatcher.

## Rule priority and Ponytail

Prompts contain only relevant shared rules, not the full rules document. The
effective safety order is project business facts and acceptance criteria, then
AI Development Manager scope/do-not-touch/regression requirements, then the
optional Ponytail minimal-change preference. Ponytail is enabled only for an
opted-in coding/repo-edit task. If the local skill is unavailable, the prompt
uses the equivalent short text policy and continues.
