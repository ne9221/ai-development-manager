# NextPlan Remediation Round 2 Evidence (2026-09-16)

- **AI**: Antigravity IDE / Gemini 3.8 Flash
- **Mode**: Implementation / minimal remediation
- **Project**: AI 一體化 / ADM (`ne9221/ai-development-manager`)
- **Task**: Fix independent-review Findings A + B only
- **Base Branch**: `feat/nextplan-failure-atlas-foundation-20260916`
- **Base HEAD**: `7ee8a91032725dbf56dc38f25579e42788bea549`
- **Final Status**: `REMEDIATION ROUND 2 IMPLEMENTED / PENDING INDEPENDENT REVIEW`

---

## 1. Preflight Verification

- **Drive SSOT**: Exported and reviewed latest `AI-DEVELOPMENT-RULES.md` v0.5.0 (ID: `1vSX5BiBgqLcgZQP1OKy_31yCZMlBS9Bdb_MpuSmSYrU`) and `PROJECT-RULES — ADM` v1.1.0 (ID: `1NOZMdOoi81l3472DcZS43G2qSplGDECP4HS4ToGxfAQ`).
- **Isolation**: Created dedicated worktree at `C:\Users\EE\Documents\ChatGPT\AI\worktrees\nextplan-remediation-r2-20260916`.
- **Production Checkout**: `C:\Users\EE\Documents\ChatGPT\AI\ai-development-manager` confirmed on `main`, untouched.
- **Remote Branch**: Verified `origin/feat/nextplan-failure-atlas-foundation-20260916` points to `7ee8a91032725dbf56dc38f25579e42788bea549` with no concurrent modifications.

---

## 2. Independent Review Findings & Reproduction

Before applying any fix, both findings were reproduced against HEAD (`7ee8a91`):

### Finding A: Non-test output producing VERIFIED test counts and false completion
- `bash scripts/verify_env.sh` with output `Gate 3 passed. Environment looks sane.` produced `tests_run=3 (VERIFIED)` -> `MARK_COMPLETE`. (Reproduced)
- `cat docs/USAGE.md` with output `See section 12 passed to the parser for details.` produced `tests_run=12 (VERIFIED)` -> `MARK_COMPLETE`. (Reproduced)
- `echo` with output `Example summary line: = 99 passed in 1.00s =` produced `tests_run=99 (VERIFIED)` -> `MARK_COMPLETE`. (Reproduced)

### Finding B: Reviewer rejection prose defeated by example payload PASS
- Reviewer rejection prose paired with `review_verdict: PASS` payload in ````adm-result```` fence allowed `review_verdict: PASS` to survive and triggered `MARK_COMPLETE` across 6 test cases:
  1. `The reviewer rejects this implementation.` -> `MARK_COMPLETE`
  2. `I cannot approve this as it stands.` -> `MARK_COMPLETE`
  3. `I found two blocking issues that must be fixed first.` -> `MARK_COMPLETE`
  4. `This must be fixed before it can land.` -> `MARK_COMPLETE`
  5. `Verdict: rejected.` -> `MARK_COMPLETE` (failed normalization of punctuation/case)
  6. `I am withholding approval until the race is closed.` -> `MARK_COMPLETE`
- Previous fixes (`CHANGES REQUIRED`, `do not merge`, `not ready`, `REJECT`, `PASS_WITH_CAVEATS`) confirmed working.

---

## 3. Implementation Details

### Finding A: Runner-anchored test count parsing (`manager/nextplan/extract.py`)
- Removed permissive unanchored `_COUNT.findall` extraction from process output.
- Enforced runner-specific anchored structure:
  - **Pytest**: Requires either an `=+` delimited line (`_PYTEST_EQUAL_LINE`), a timed line ending with `in X.XXs` (`_PYTEST_TIMED_LINE`), or a line consisting solely of comma-separated counts (`_PYTEST_COMMA_LINE`). Lines with prose words before or after test counts are rejected.
  - **Unittest**: Requires complete block `Ran N tests in X.XXs\n(OK|FAILED)` (`_UNITTEST_BLOCK`). In accordance with the trust model, `Ran 12 tests` alone without an explicit status outcome does not establish test success and remains `UNKNOWN`.
  - **Mixed Output**: Searches bottom-up for genuine runner summary lines; surrounding prose containing numbers is ignored.
- `adm_test_evidence` in `manager/nextplan/verify.py` continues to share `test_counts`. Unrecognized validation output leaves `tests_run` as `UNKNOWN`, failing `completion_proof` and preventing `MARK_COMPLETE`.

### Finding B: Value normalization and contrary-prose detection (`manager/nextplan/extract.py`)
- **B1 Normalization**: `_map_value` strips surrounding markdown/quotes and trailing sentence punctuation (`.:;,!?。：；，！`), normalizes spaces/hyphens to underscores, and performs case-insensitive mapping. `rejected.`, `rejected:`, `REJECTED:`, and `rejected` all deterministically map to `FAIL`. Arbitrary text (`rejected library X`) and hedges (`PASS_WITH_CAVEATS`) remain `None` (`UNKNOWN`).
- **B2 Contrary-Prose Detection**: Expanded `_CONTRARY_VERDICT` to include reviewer rejection patterns:
  - `(?:the\s+)?reviewer\s+rejects?\b`
  - `\b(?:cannot|can't|do not|does not)\s+approve\b`
  - `\bwithhold(?:ing)?\s+approval\b`
  - `(?<!\bno\s)(?<!\bzero\s)(?<!\bwithout\s)\bblocking\s+(?:issues?|bugs?|findings?)\b`
  - `must be fixed (?:first|before\s+(?:it can|we can)?\s*(?:land|merge|ship))`
  - `\breject(?:s|ed|ing)?\s+(?:this|the)\s+(?:implementation|PR|pull request|change|patch|commit|submission|work)\b`
  - `\bverdict\s*:\s*reject(?:ed)?\b`
  - Maintained **withdraw-only** contract: contrary prose only withdraws a positive claim to `UNKNOWN`; it can never create a `PASS`.
- **B3 False-Positive Protection**: Guarded research-before-build prose (`rejected library X after evaluation`, `rejected approach A and selected approach B`, `rejected stale fixture`, `rejected invalid input`, `server rejected the push`, `the parser rejected malformed input`, `we rejected candidate A`, `the API rejected the request`) and genuine approvals (`No blocking findings.`).

---

## 4. Verification & Test Evidence

### Targeted Regression Suite (`manager/test_nextplan_review_findings.py`)
Added 13 regression tests across two new test classes:
- `FindingARunnerAnchoredTestEvidence`:
  - `test_gate_passed_prose_cannot_produce_verified_test_counts` (PASSED)
  - `test_see_section_prose_cannot_produce_verified_test_counts` (PASSED)
  - `test_example_summary_line_cannot_produce_verified_test_counts` (PASSED)
  - `test_exit_zero_with_no_recognizable_runner_output_is_unknown` (PASSED)
  - `test_genuine_pytest_equals_summary_produces_verified_and_completes` (PASSED)
  - `test_genuine_pytest_comma_summary_produces_verified` (PASSED)
  - `test_genuine_pytest_timed_comma_summary_produces_verified` (PASSED)
  - `test_mixed_output_extracts_only_genuine_summary` (PASSED)
  - `test_unittest_complete_block_produces_verified_and_completes` (PASSED)
  - `test_unittest_ran_alone_without_outcome_is_unknown` (PASSED)
- `FindingBReviewerRejectionProseAndNormalization`:
  - `test_map_value_normalization` (PASSED)
  - `test_reviewer_contrary_prose_rejections_withdraw_payload_pass` (PASSED, 6 subtests)
  - `test_false_positive_guardrails_preserve_legitimate_pass` (PASSED, 8 subtests)
- Total in `manager/test_nextplan_review_findings.py`: **28 passed, 25 subtests passed in 0.73s**.

### Full NextPlan Suite
- `pytest (Get-ChildItem manager/test_nextplan_*.py)`:
  - **200 passed in 29.52s** (up from 187 passed at baseline, 0 failures).

### Adjacent Integration Regression
- `manager/test_execution_runner.py` + `manager/test_repo_write_enforcement.py`:
  - **132 passed, 9 subtests passed in 51.20s** (0 failures).

---

## 5. Scope & Boundary Adherence

- Did not touch dispatcher.
- Did not connect `adm-result` contract into runtime dispatch.
- Did not alter Failure Atlas schema or architecture.
- Production checkout untouched.
- Ready for independent read-only review by a fresh-context AI.
