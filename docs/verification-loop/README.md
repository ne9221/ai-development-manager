# Verification Loop — design and review lineage

This directory is the GitHub-side durable copy of the Verification Loop design
and review record. Google Drive (ADM project folder
`16MO98FfbnwXsin-m6F1rgdmk-o6ZslVC`) remains the human-readable governance
store per AI-DEVELOPMENT-RULES v0.5.0 rules 25/29; these files exist so the
same text is also version-controlled, diffable and hash-verifiable.

Until 2026-09-06 the Phase A v3 architecture and the Phase B-1 independent
review existed **only** in local Claude Code session transcripts. Both
independent reviewers flagged that as a durable-record gap. This directory
closes it.

## Lineage

| Stage | Artifact | Verdict |
|---|---|---|
| Phase A v3 | [PHASE-A-V3-ARCHITECTURE.md](PHASE-A-V3-ARCHITECTURE.md) | `READY_FOR_PHASE_B` |
| B-1 implementation | commit `109be49` (`manager/verification_loop/`) | — |
| B-1 independent review | [PHASE-B1-INDEPENDENT-REVIEW-CHANGES-REQUIRED.md](PHASE-B1-INDEPENDENT-REVIEW-CHANGES-REQUIRED.md) | `CHANGES_REQUIRED` |
| B-1R repair | commit `62b403b` | — |
| B-1R independent re-review | [PHASE-B1R-INDEPENDENT-REVIEW-ACCEPTED.md](PHASE-B1R-INDEPENDENT-REVIEW-ACCEPTED.md) | `PHASE_B1_ACCEPTED` |
| B-2 implementation | this branch | pending independent review |

## Provenance

`PHASE-A-V3-ARCHITECTURE.md` is the **verbatim, unedited** final assistant
message of the Opus session titled "Verification Loop Acceptance Layer Phase A
架構", transcript
`~/.claude/projects/C--Users-EE--ai-development-manager/5ad214ad-a62a-4ccc-b16a-1655360fd6d2.jsonl`,
message index 93. It was extracted programmatically and **not** rewritten,
summarised, corrected or re-scoped. Where Phase B-1/B-1R deviate from it, the
deviation is recorded in the review documents, never by editing this file.

The two review files are likewise the verbatim final verdict messages of their
respective review sessions (`f22f728a-…` index 250, `3baee210-…` index 389).

Recorded digests (sha256 of the file bytes as committed):

| File | Bytes | sha256 |
|---|---|---|
| `PHASE-A-V3-ARCHITECTURE.md` | 46055 | `2c5dc883a7039de60433f73f187a063babad8db8071cf875b778bce81df7903c` |
| `PHASE-B1-INDEPENDENT-REVIEW-CHANGES-REQUIRED.md` | 9327 | `8462d21a7d8523ad02e7ffa224b2553045c79a780e9232f92a699ae242cb950a` |
| `PHASE-B1R-INDEPENDENT-REVIEW-ACCEPTED.md` | 5028 | `adbf856eeba391386266bce1516c27b2d1c5cde71dac7d552d73b69cfa9ed7c0` |

## Governance sources these documents are bound to

- `AI-DEVELOPMENT-RULES` — Google Drive `1vSX5BiBgqLcgZQP1OKy_31yCZMlBS9Bdb_MpuSmSYrU`, **v0.5.0**, 2026-09-01.
- `PROJECT-RULES — ADM` — Google Drive `1NOZMdOoi81l3472DcZS43G2qSplGDECP4HS4ToGxfAQ`, **v1.1.0**, 2026-08-21.

The repository's own top-level `AI-DEVELOPMENT-RULES.md` is a **stale v0.1.5
copy** and is not authoritative. Phase A v3 section B hard-codes
`github_copy_authoritative: false` for exactly this reason.
