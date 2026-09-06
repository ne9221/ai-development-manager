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
| B-2 implementation | commit `794db70` | — |
| B-2 independent review | [PHASE-B2-INDEPENDENT-REVIEW-CHANGES-REQUIRED.md](PHASE-B2-INDEPENDENT-REVIEW-CHANGES-REQUIRED.md) | `CHANGES_REQUIRED` |
| B-2R repair | this branch | pending focused independent re-review |

## Reproducers

`repro/B2R-ADVERSARIAL-AUDIT.py` is the Phase B-2R self-adversarial audit: 11
attacks and 2 live controls, runnable at either SHA from a scratch clone with a
temporary `AI_MANAGER_HOME`:

```
PYTHONPATH=. python docs/verification-loop/repro/B2R-ADVERSARIAL-AUDIT.py <scratch-dir>
```

It is committed rather than left in a scratch directory so the next reviewer can
re-measure the claim instead of taking it. Measured: **7 of 11 bypass at
`794db70`, 0 of 11 at the B-2R HEAD, with both controls still accepting.** The
four that fail to bypass at base do so through guards Phase B-2 already had, and
the script says so rather than counting them as wins.

The script is deliberately layout-agnostic: it finds ticket records by reading
the `ticket_id` inside them rather than by any filename convention. An earlier
version assumed the layout, silently matched nothing after the ledger changed
shape, and scored four attacks as bypasses that had never touched a byte.

## Provenance

`PHASE-A-V3-ARCHITECTURE.md` is the **verbatim, unedited** final assistant
message of the Opus session titled "Verification Loop Acceptance Layer Phase A
架構", transcript
`~/.claude/projects/C--Users-EE--ai-development-manager/5ad214ad-a62a-4ccc-b16a-1655360fd6d2.jsonl`,
message index 93. It was extracted programmatically and **not** rewritten,
summarised, corrected or re-scoped. Where Phase B-1/B-1R deviate from it, the
deviation is recorded in the review documents, never by editing this file.

The three review files are likewise the verbatim final verdict messages of
their respective review sessions (`f22f728a-…` index 250, `3baee210-…` index
389, `b73955eb-3926-4931-bbfc-3c9c9dfd7a62` index 474). Verbatim means
verbatim: the B-2 file's closing line is the reviewer's conversational offer
to publish, kept rather than trimmed, because an implementer editing a
reviewer's verdict — even to tidy it — is the thing this directory exists to
make impossible.

Recorded digests (sha256 of the file bytes as committed):

| File | Bytes | sha256 |
|---|---|---|
| `PHASE-A-V3-ARCHITECTURE.md` | 46055 | `2c5dc883a7039de60433f73f187a063babad8db8071cf875b778bce81df7903c` |
| `PHASE-B1-INDEPENDENT-REVIEW-CHANGES-REQUIRED.md` | 9327 | `8462d21a7d8523ad02e7ffa224b2553045c79a780e9232f92a699ae242cb950a` |
| `PHASE-B1R-INDEPENDENT-REVIEW-ACCEPTED.md` | 5028 | `adbf856eeba391386266bce1516c27b2d1c5cde71dac7d552d73b69cfa9ed7c0` |
| `PHASE-B2-INDEPENDENT-REVIEW-CHANGES-REQUIRED.md` | 11300 | `59f9b6607b479dda8c1ea29894a8a491f6bc8d5406778bbd7a250596908a8a02` |

## Governance sources these documents are bound to

- `AI-DEVELOPMENT-RULES` — Google Drive `1vSX5BiBgqLcgZQP1OKy_31yCZMlBS9Bdb_MpuSmSYrU`, **v0.5.0**, 2026-09-01.
- `PROJECT-RULES — ADM` — Google Drive `1NOZMdOoi81l3472DcZS43G2qSplGDECP4HS4ToGxfAQ`, **v1.1.0**, 2026-08-21.

The repository's own top-level `AI-DEVELOPMENT-RULES.md` is a **stale v0.1.5
copy** and is not authoritative. Phase A v3 section B hard-codes
`github_copy_authoritative: false` for exactly this reason.
