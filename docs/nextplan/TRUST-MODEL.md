# NextPlan trust model

Status: Remediation Round 4 (2026-09-16), pending independent review.
Applies to `manager/nextplan/extract.py`, `verify.py` and `classify.py`.

Every earlier round in this branch failed the same way: something was allowed to
mean approval *by default*. Round 2 let a documentation example prove tests ran.
Round 3 let a payload say `review_verdict: PASS` and treated "no rejection
matched" as the reviewer agreeing — so 26 of 30 reworded rejections completed,
and so did a payload with no prose at all.

The rule underneath all four answers below is therefore the same:

> **Silence is not consent, and an unreadable statement is not silence.**
> Nothing becomes a positive claim because nothing contradicted it.

## 1. What may establish a reviewer PASS?

Only an **explicit authoritative decision the reviewer wrote**:

- a key/value decision line — `Verdict: PASS`, `Final review: APPROVED`,
  `Decision: APPROVED`, `Review result: PASS`, `審查結果：PASS`;
- in an **authoritative region** — not inside a fence, a quote, an indented
  block or a documentation example;
- mapping **exactly** onto the verdict enum (`PASS_WITH_CAVEATS` is not `PASS`);
- with no conflicting decision elsewhere in the message;
- from an event whose role is genuinely `REVIEWER` (ADM's dispatch record says
  so, never the agent).

A structured payload may *carry* `review_verdict`, but it cannot authorize a
PASS on its own. Absent an explicit decision the fact is `UNKNOWN`, and
`classify` raises `result.required_field_missing` rather than completing.

The asymmetry is deliberate: **`FAIL` needs no anchor.** A payload asserting
failure only ever routes away from completion, so believing it is safe.
Gating it would be a way to lose a rejection, not a way to avoid a false pass.

Withdrawal is a separate, second line of defence. Rejection prose can take a
stated PASS away; it can never grant one. That ordering is what makes a missed
rejection wording cost a round instead of completing a task.

## 2. What may establish VERIFIED `tests_run`?

Only a **test-designated execution ADM itself recorded**:

- `is_test_step()` accepts a step when its explicit `kind` is `test`, or — until
  the execution runner emits that field — when the Task's declared
  `validation_command` is recognisably a test-runner invocation
  (`is_test_command()` reads the *program* of each shell segment, not any
  substring of the line);
- an explicit `kind` always wins, in both directions: `kind: lint` on a `pytest`
  command is not a test run;
- only then is `output_summary` parsed for counts, and only then can
  `TestEvidenceProbe` observe them.

A step that is not test-designated still records its exit code — a failure is
never lost — but it cannot say how many tests ran. `true` exits 0; so does a
script that prints a convincing transcript. Codex reproduced exactly that,
escalating a documented `8 passed` into `VERIFIED tests_run=8` and a completed
task.

Agent prose never reaches VERIFIED at all. It can reach `REPORTED` at best, and
the completion proof requires VERIFIED, so a pasted transcript cannot complete a
task however genuine it looks. That is the backstop the residual in §4 rests on.

## 3. Which payloads are authoritative?

None of them, by themselves.

| Source | May assert | Ceiling |
|---|---|---|
| `probe:*` (git, ADM execution record, Drive read-back, test evidence) | anything it observed | `VERIFIED` / `CONTRADICTED` |
| native / fenced `adm-result` payload | field values; `FAIL`-direction verdicts | `REPORTED` |
| authoritative key/value lines | field values; an explicit reviewer decision | `REPORTED` |
| constrained heuristics | status only, when nothing structured was attempted | `DERIVED` |
| fenced, quoted, indented or documented regions | **nothing** | — |

ADM's own dispatch record — never the agent — supplies `task_id`, `role` and
`session_id`.

## 4. What happens on ambiguity?

Fail closed, every time.

- Normalization that does not converge inside its cap returns **no value**, and
  an unreadable *authoritative* verdict raises `extract.conflicting_statements`
  so it cannot pass for silence.
- Two sources that disagree make the fact `UNKNOWN` and signal the conflict;
  disagreement is never resolved optimistically.
- A payload that was attempted and is malformed stops the heuristic tier for
  status — ADM asks again rather than reading "looks done" into prose.
- An unterminated fence is treated as documentation to the end of the message.
- A missing proof item is a **failing** item, never an absent one. Round 2's
  original defect was a requirement that vanished when nothing could satisfy it;
  Round 3 reintroduced the same shape one level up, in a test that asserted a
  verdict was unreadable but never that the task could not complete.

### Known residual

A runner session pasted with no lead-in — or with a blank line between the
lead-in and the summary line — is textually identical to the legitimate
"example, blank line, then the real result" case. No rule can separate them from
the text alone, so the parser still reads it.

It is contained rather than solved, in two independent ways: such text is agent
prose, which is capped at `REPORTED`; and counts are only read at all from a
test-designated step. Both would have to fail together for it to matter.
