# Governance drift audit — read-only proposal (2026-09-16)

Produced during the NextPlan foundation task as its Backup 3 work item.
**Read-only**: nothing in Drive and nothing in `AI-DEVELOPMENT-RULES.md` was
modified. This document proposes; a human decides.

## What was compared

| Copy | Version | Rules | Sections | Measured |
|---|---|---|---|---|
| Drive `AI-DEVELOPMENT-RULES.md` (`1vSX5Bi…SYrU`) | 0.5.0, 2026-09-01 | 47 | 12 | exported at task preflight |
| GitHub `ai-development-manager/AI-DEVELOPMENT-RULES.md` @ `2a004d4` | 0.1.5, 2026-08-29 | 18 | 3 | read from the branch base |

## The finding

The two copies are four minor versions and 29 rules apart, and the stale one
is the copy an AI is most likely to read first.

The repo copy opens with **"Single source of truth for cross-project
AI-development rules"**. That sentence is false as written, and it is the
active risk: an agent that preflights from the checkout believes it has the
governance and never learns that it is missing, among others:

- §9 Global Hands-off Execution Layer (rules 38–45), including rule 44's dual
  fresh-conversation write-E2E acceptance requirement;
- §10 Production Fix Protocol (rules 46–47), the phased review/fix discipline
  and the failure-pattern library;
- §2 rule 11 (never guess quota) and §5 rule 24 (reproduce at base before
  claiming a fix);
- the Quota source baseline.

This is not a hypothetical: the ADM memory index already carries two separate
corrections recording that an earlier session cited the GitHub copy's rule
numbering and was wrong.

## Proposal (needs human approval; none of it is done)

1. **Stop the copy claiming to be the SSOT.** Replace the repo file's opening
   sentence with a pointer: the canonical rules are the Drive document, this
   copy is a convenience snapshot, and its version line states which Drive
   revision it was taken from.
2. **Either sync or stub.** Either regenerate the repo copy from Drive v0.5.0
   as part of a governance task, or reduce it to a stub containing only the
   pointer and the Drive file id. A stub is the safer of the two, because a
   synced copy silently goes stale again on the next Drive edit.
3. **Make staleness detectable.** If a copy is kept, record the Drive
   `modifiedTime` and version in it, and have a check compare them (ADM already
   reads Drive with the same credential the preflight used).
4. **Leave the Drive document alone.** It is the SSOT and the current task has
   no authority to edit it.

## Scope note

The repo governance file is not part of the NextPlan task's allowed
modification scope, so this audit deliberately stops at a proposal. No file was
edited, and the Drive SSOT was read only.
