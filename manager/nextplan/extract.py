"""Agent output -> normalized result, without ever inventing success.

Sources are tried in a fixed order and each one caps the evidence level it can
produce:

  1. native      the output *is* a report object (``format: json``) or a
                 stream-json result event carries ``structured_output``  REPORTED
  2. fenced      a ```adm-result block (or a ```json block whose object has
                 ``schema_version: adm-ai-result/1``)                     REPORTED
  3. deterministic  anchored key/value lines and pytest/unittest summaries REPORTED
  4. heuristic   constrained keyword rules, only when nothing structured
                 was even attempted                                        DERIVED
  5. none        everything UNKNOWN

Nothing here can produce VERIFIED; only manager.nextplan.verify can.

The trust model these tiers implement is written down in
docs/nextplan/TRUST-MODEL.md: what may establish a reviewer PASS, what may
establish VERIFIED test counts, which payloads are authoritative, and what
happens on ambiguity. One rule underlies all four -- silence is not consent,
and an unreadable statement is not silence. In particular a reviewer's PASS
needs an explicit decision the reviewer wrote; a payload may carry the value
but may not authorize it, because treating "no rejection matched" as approval
made every wording the matcher missed into an approval.

Two rules keep this from being permissive:

- **Disagreement is never resolved optimistically.** When the payload and a
  deterministic line (or two deterministic lines) disagree about a field, the
  field becomes UNKNOWN and ``extract.conflicting_statements`` is signalled.
- **A broken structured attempt is not guessed at.** If the agent tried to emit
  a payload and it was invalid or truncated, the heuristic tier does not run
  for status: we ask again rather than read "looks done" into prose.

Negative heuristics (quota / rate-limit phrases) always run: acting on them
only ever routes away from completion.
"""

from __future__ import annotations

import hashlib
import json
import re

from jsonschema import Draft202012Validator

from manager.nextplan import contracts
from manager.nextplan import result as r
from manager.nextplan import vocabulary as v

_FIELD_VALIDATORS = {field: Draft202012Validator(r._value_schema(kind)) for field, kind in r.FIELD_KINDS.items()}
_TIER_ORDER = {tier: index for index, tier in enumerate(r.TIERS)}

# Facts only a reviewer has the authority to assert, whatever tier they arrive in.
REVIEW_AUTHORITY_FIELDS = frozenset({"review_verdict", "reviewed_sha"})

_FENCE_LINE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>`{3,}|~{3,})[ \t]*(?P<info>[^\n]*?)[ \t]*$")
_PAYLOAD_LANGS = frozenset({"adm-result", "json"})
# A reviewer's decision travels in its own fenced block, separate from the
# result report: the report is the reviewer's *claims*, the decision is the
# reviewer's *authority*, and mixing the two is what let a claim authorize.
_DECISION_LANG = "adm-review-result"
_HEX = re.compile(r"\b[0-9a-f]{7,40}\b")

# -- evidence provenance --------------------------------------------------------
#
# Execution evidence must come from output the agent presents as its own run, so
# three regions are never read as evidence:
#
#   * anything inside a code fence, whatever its language;
#   * markdown quote lines (the agent is citing, not reporting);
#   * a block introduced by a documentation lead-in ("Example:", "Sample
#     output:", "the format looks like this:").
#
# The split is asymmetric on purpose. *Evidence narrows* to authoritative
# regions, because a documented example must not be able to prove anything.
# *Withdrawal widens* to everything outside the agent's own payload, because a
# rule that can only remove a claim is safe to run everywhere -- narrowing it
# would just open a new bypass ("put the REJECT inside a fence").
_QUOTE_LINE = re.compile(r"^\s{0,3}>")
# A markdown indented code block (4 spaces or a tab). Codex found that an
# indented transcript was still read as this run's output.
_INDENTED_LINE = re.compile(r"^(?: {4,}|\t)\S")
_LEAD_IN = re.compile(r"[:：]\s*$")
_EXAMPLE_WORDS = re.compile(
    r"\b(?:examples?|samples?|e\.g\.|for instance|illustrat\w*|hypothetical|mock|dummy|"
    r"template|schema|format|looks like|reference|docs?|documentation|pseudo\w*|"
    r"transcripts?|sessions?|snippets?|excerpts?|expected(?:\s+\w+)?\s+output|console)\b|"
    r"範例|示例|例如|格式|參考",
    re.I
)
# A documentation block runs to the next blank line, and never further than this
# many lines: a lead-in with no blank line after it must not swallow a whole
# report. Losing evidence only ever costs a completion, it never grants one.
_EXAMPLE_MAX_LINES = 40


def _fence_regions(lines):
    """[(lang, body, closed, indexes)] for every fenced block, whatever its language.

    A closing fence must use the same character as its opener and be at least as
    long, and must carry no info string. Comparing only the character let a
    three-backtick line close a ````text block, and the body leaked out as if it
    were the agent's own output.
    """
    regions, index = [], 0
    while index < len(lines):
        opened = _FENCE_LINE.match(lines[index])
        if not opened:
            index += 1
            continue
        marker, start = opened.group("marker"), index
        info = opened.group("info").strip()
        body, index = [], index + 1
        while index < len(lines):
            closing = _FENCE_LINE.match(lines[index])
            if (closing and not closing.group("info")
                    and closing.group("marker")[0] == marker[0]
                    and len(closing.group("marker")) >= len(marker)):
                break
            body.append(lines[index])
            index += 1
        closed = index < len(lines)
        # Only the first token of the info string is the language: a fence may
        # carry attributes (```console title="example").
        lang = info.split()[0] if info else ""
        regions.append((lang, "\n".join(body), closed, range(start, min(index + 1, len(lines)))))
        index += 1
    return regions


def non_evidence_lines(lines):
    """Line indexes that must never be read as *this* run's execution evidence."""
    skip = set()
    for _info, _body, _closed, indexes in _fence_regions(lines):
        skip.update(indexes)
    for index, line in enumerate(lines):
        if _QUOTE_LINE.match(line) or _INDENTED_LINE.match(line):
            skip.add(index)
    for index, line in enumerate(lines):
        if index in skip or not _LEAD_IN.search(line) or not _EXAMPLE_WORDS.search(line):
            continue
        skip.add(index)
        cursor = index + 1
        while cursor < len(lines) and not lines[cursor].strip():  # "Example:" then a blank line
            cursor += 1
        limit = cursor + _EXAMPLE_MAX_LINES
        while cursor < len(lines) and cursor < limit and lines[cursor].strip():
            skip.add(cursor)
            cursor += 1
    return skip

# -- deterministic vocabulary ---------------------------------------------------

_KEY_ALIASES = {
    "status": "status", "狀態": "status", "result": "status", "結果": "status", "final status": "status",
    "最終狀態": "status", "verdict": "review_verdict", "review verdict": "review_verdict",
    "reviewer verdict": "review_verdict", "審查結果": "review_verdict",
    "decision": "review_verdict", "review decision": "review_verdict",
    "final decision": "review_verdict", "final review": "review_verdict",
    "final verdict": "review_verdict", "review result": "review_verdict",
    "審查結論": "review_verdict", "結論": "review_verdict",
    "commit": "commit_sha", "commit sha": "commit_sha", "commit_sha": "commit_sha",
    "head": "head_sha", "head sha": "head_sha", "head_sha": "head_sha", "local head": "head_sha",
    "remote sha": "remote_sha", "remote_sha": "remote_sha", "remote head": "remote_sha",
    "base": "base_sha", "base sha": "base_sha", "base_sha": "base_sha",
    "reviewed sha": "reviewed_sha", "reviewed_sha": "reviewed_sha",
    "branch": "branch", "分支": "branch",
    "push": "push_status", "push status": "push_status", "push_status": "push_status", "github sync": "push_status",
    "git status": "git_status", "git_status": "git_status",
    "tests": "tests", "test": "tests", "測試": "tests",
    "drive sync": "ssot_sync_drive", "drive": "ssot_sync_drive",
    "progress": "progress_percent", "進度": "progress_percent", "目前進度": "progress_percent",
    "blockers": "blockers", "blocker": "blockers",
}
_KV_LINE = re.compile(r"^\s*(?:[-*•]\s*)?(?:\*\*)?(?P<key>[A-Za-z_ 一-鿿]{1,24}?)(?:\*\*)?\s*[:：]\s*(?P<val>.*?)\s*$")

_STATUS_WORDS = {
    "PASS": "PASS", "PASSED": "PASS", "SUCCESS": "PASS", "OK": "PASS", "成功": "PASS", "通過": "PASS",
    "DONE": "PASS", "COMPLETE": "PASS", "COMPLETED": "PASS", "完成": "PASS", "已完成": "PASS",
    "READY_FOR_INDEPENDENT_REVIEW": "PASS",
    "FAIL": "FAIL", "FAILED": "FAIL", "FAILURE": "FAIL", "失敗": "FAIL",
    "PARTIAL": "PARTIAL", "PARTIALLY_READY": "PARTIAL", "部分完成": "PARTIAL",
    "BLOCKED": "BLOCKED", "阻塞": "BLOCKED",
    "IN_PROGRESS": "IN_PROGRESS", "進行中": "IN_PROGRESS",
    "ERROR": "ERROR",
}
_VERDICT_WORDS = {"PASS": "PASS", "APPROVE": "PASS", "APPROVED": "PASS", "ACCEPTED": "PASS",
                  "FAIL": "FAIL", "REJECT": "FAIL", "REJECTED": "FAIL", "CHANGES_REQUIRED": "FAIL"}
_NONE_WORDS = re.compile(r"^(none|n/?a|無|沒有|-|—)\.?$", re.I)

_COUNT = re.compile(r"(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed)\b")
# "subtests" is real pytest-subtests output ("187 passed, 954 subtests passed in
# 23.22s"). It is recognised here so the line parses at all, but it is
# deliberately absent from _COUNT: the primary population is 187, and the 954
# must not be added to it nor read as a second run.
_COUNT_WORD = (r"(?:sub(?:test|tests)\s+(?:passed|failed|skipped)|passed|failed|skipped|errors?|"
               r"xfailed|xpassed|warnings?|deselected)")

_PYTEST_EQUAL_LINE = re.compile(
    r"^=+\s*(?:short test summary info\s*=+\s*\n\s*)?(?P<counts>(?:\d+\s+%s(?:,\s*)?)+)"
    r"(?:\s+in\s+[\d.]+s.*?)?\s*=+$" % _COUNT_WORD,
    re.I
)
_PYTEST_TIMED_LINE = re.compile(
    r"^(?P<counts>\d+\s+%s(?:,\s*\d+\s+%s)*)\s+in\s+[\d.]+s.*$" % (_COUNT_WORD, _COUNT_WORD),
    re.I
)
_PYTEST_COMMA_LINE = re.compile(
    r"^(?P<counts>\d+\s+%s(?:,\s*\d+\s+%s)+)\s*$" % (_COUNT_WORD, _COUNT_WORD),
    re.I
)
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in [\d.]+s\s*$")
_UNITTEST_FAILED = re.compile(r"^FAILED \((.*)\)\s*$")
_UNITTEST_STATUS = re.compile(r"^(?:OK(?:\s*\((?P<ok>.*?)\))?|FAILED\s*\((?P<bad>.*?)\))$")

# -- heuristic vocabulary (constrained) ---------------------------------------------

_QUOTA = re.compile(r"usage limit|quota (?:is )?(?:exhausted|exceeded)|insufficient_quota|out of credits|"
                    r"free-models-per-day|額度(?:已)?(?:用完|耗盡)", re.I)
_RATE = re.compile(r"\b429\b|rate[- ]?limit(?:ed)?", re.I)
_BLOCKED_PROSE = re.compile(r"\bblocked\b|\bcannot proceed\b|無法繼續|阻塞", re.I)
_DONE_PROSE = re.compile(r"\ball (?:tests )?pass(?:ed|ing)?\b|\b(?:done|completed|finished)\b|已完成|完成", re.I)
_FAIL_PROSE = re.compile(r"\bfail(?:ed|ing|ure)?\b|失敗", re.I)
# -- structural rejection detection (withdraw-only) ---------------------------
#
# An earlier round matched whole sentences, so every reworded rejection was a
# new false pass and every fix was another literal bolted on. This reads
# *structure* instead: a small set of composed families, each a (polarity,
# target) pair, evaluated per clause with negation resolved inside the clause.
#
#   F1  an approval term that is negated, withheld or made conditional
#   F2  a blocker asserted to exist (guarded: "no blocking issues" is not one)
#   F3  a fix ordered before landing (guarded: "nothing must be fixed first")
#   F4  another round, or the work sent back to its author
#   F5  a rejection whose object is the work under review -- never a library,
#       an approach, a fixture, a push or malformed input, which is how ADM
#       reports record research-before-build and ordinary failures
#
# Guarded families consult _negated, a clause-local window, rather than
# a lookbehind: "there are no remaining blocking issues" defeated the fixed
# lookbehind simply by putting a word between "no" and "blocking".
#
# This is withdraw-only. It can turn a PASS into UNKNOWN; nothing here can ever
# assert one, so a false negative costs a re-ask and a false positive costs a
# round -- and the guards exist because a false positive on honest prose is
# itself a defect.
_CLAUSE_SPLIT = re.compile(r"[.;!?\n。；！？]+|,\s")
_WORD = re.compile(r"[\w'’-]+")
_NEGATOR = re.compile(r"^(?:no|not|n't|never|zero|none|nothing|without|neither|nor|non|0)$", re.I)
_NEG_WINDOW = 4
# A short match leaves its predicate outside the span; read a little of it.
_PREDICATE_WINDOW = 2

# Explicit negation carriers. Bare "no" is deliberately absent: it is the
# quantifier in "no blocking issues", which F2 already handles, and admitting it
# here would read "No issues block merge" as a refusal to merge.
# "am not" and "not able to" close two gaps in a *closed grammatical class*,
# not in a vocabulary of rejections: every other copula negation (is not, are
# not, was not, were not) was already here, and "unable to" was already here
# without its analytic form. Round 6's residual contained "I am not able to
# approve the patch", which is first-person decision shape that only escaped
# because English's first-person copula was missing from this list.
_NOT = (r"(?:not|cannot|can\s*not|can't|won't|will\s+not|would\s+not|shall\s+not|should\s+not|"
        r"must\s+not|may\s+not|do\s+not|does\s+not|don't|doesn't|is\s+not|isn't|are\s+not|aren't|"
        r"am\s+not|ain't|has\s+not|have\s+not|hasn't|haven't|was\s+not|were\s+not|never|"
        r"unable\s+to|not\s+able\s+to|refus\w+\s+to|declin\w+\s+to|fail\w*\s+to)")
_FILLER = r"(?:be|been|being|get|got|yet|now|ever|fully|formally|hereby|explicitly|currently|\w+ly)"
_APPROVE = (r"(?:approv\w*|accept\w*|sign[-\s]?off|signed[-\s]?off|merg\w*|land(?:ed|ing|s)?|"
            r"ship(?:ped|ping|s)?|releas\w*|grant\w*|endors\w*)")
_APPROVAL_NOUN = r"(?:approval|sign[-\s]?off|acceptance|go[-\s]?ahead)"
# Predicates that describe the *state* of an approval. Split from bare negation
# because "Approval is not conditional" negates the state and is an approval,
# while "Approval is conditional" is not.
_APPROVAL_STATE = (r"(?:withheld|withhold\w*|denied|deny|refused|refus\w*|pending|conditional|contingent|"
                   r"depend\w*|blocked|revoked|premature|deferred|postponed|suspended|on\s+hold)")
_APPROVAL_ABSENT = r"(?:not|never|no)"
_RESOLVED = (r"(?:resolved|fixed|closed|cleared|addressed|gone|eliminated|removed|corrected|repaired|"
             r"remediated|done|complete|completed|landed|merged|passing)")
_ORDERED = r"(?:must|should|needs?\s+to|has\s+to|have\s+to|will\s+need\s+to|shall|to\s+be|please)"
# A judgement attributed to an earlier review is a record, not this reviewer's
# decision: "The previous review rejected this; that issue is fixed" is an approval.
_HISTORICAL = (r"(?:previous|prior|earlier|last|first|original|former|round\s*\d+)\s+(?:reviews?|reviewers?|rounds?|passes|pass|attempts?|iterations?)")
_DEFECT = r"(?:issues?|bugs?|defects?|findings?|problems?|concerns?)"
# A blocker withdraws a PASS only when the sentence asserts one *exists*: either
# a persistence predicate or a counting quantifier. "The blocker was the missing
# token; resolved" names a blocker that is gone, and must not cost a round.
_PERSIST = r"(?:remain\w*|persist\w*|outstanding|unresolved|open|present|exists?|still|pending|left)"
_QUANT = r"(?:\d+|one|two|three|four|five|several|multiple|some|a|an|another|numerous|many)"
_BLOCKER = r"(?:blockers?|blocking\s+(?:\w+\s+){0,1}?%s)" % _DEFECT
_WORK = (r"(?:implementation|patch|pull\s+request|pr|changes?|changeset|commits?|submission|"
         r"branch|diff|revision|merge|work|deliverable)")
_FIXVERB = r"(?:fix\w*|resolv\w*|address\w*|correct\w*|repair\w*|remediat\w*)"
_LAND = r"(?:merg\w*|land\w*|ship\w*|releas\w*|approv\w*|accept\w*|sign[-\s]?off)"
# Handing the work back to whoever wrote it is a rejection whatever verb carries
# it, so the family is the verb's morphology rather than a set of sentences.
_HANDBACK = (r"(?:send|sent|sending|sends|hand(?:ed|ing|s)?|pass(?:ed|ing|es)?|kick(?:ed|ing|s)?|"
             r"bounc(?:e|ed|ing|es)|return(?:ed|ing|s)?|giv(?:e|en|ing)|gave|throw(?:n|ing|s)?|threw)")
_AUTHOR = r"(?:implementer|author|worker|developer|submitter|contributor)"
# What is handed back has to be the work under review: without this, "handed the
# token back to the caller" read as a rejection.
_UNDER_REVIEW = r"(?:this|it|(?:this|that|the|your)\s+(?:\w+\s+){0,1}?%s)" % _WORK

_REJECTION_RULES = tuple((re.compile(pattern, re.I), guarded) for pattern, guarded in (
    # F1 -- approval negated, withheld or conditional
    (r"\b%s\s+(?:%s\s+){0,2}%s\b" % (_NOT, _FILLER, _APPROVE), False),
    # (the approval-noun family moved to _approval_rejected: its polarity needs code)
    (r"\bwithhold\w*\s+(?:my\s+|our\s+|the\s+)?%s\b" % _APPROVAL_NOUN, False),
    (r"\bunacceptable\b|\bnot\s+acceptable\b|\bnot\s+ready\b", False),
    # F2 -- a blocker asserted to exist
    (r"\b%s\b[^,;.!?]{0,30}?\b%s\b" % (_BLOCKER, _PERSIST), True),
    (r"\b%s\s+(?:\w+\s+){0,2}?%s\b" % (_PERSIST, _BLOCKER), True),
    (r"\b%s\s+(?:\w+\s+){0,2}?%s\b" % (_QUANT, _BLOCKER), True),
    (r"\b%s\s+(?:that\s+)?block(?:s|ed|ing)?\s+(?:the\s+)?"
     r"(?:merge|merging|landing|release|approval|this|it)\b" % _DEFECT, True),
    (r"\b(?:merg\w+|landing|release|approval|this|it)\s+(?:is|are|remains?|stays?)\s+(?:\w+\s+){0,2}?blocked\b", True),
    # F3 -- a fix ORDERED before landing. Past tense without a modal reports
    #       work already done: "We fixed the bug before merging; I approve" is an
    #       approval, and Round 3 withdrew it.
    (r"\b(?:please\s+)?(?:fix|resolve|address|correct|repair|remediate|close)\b"
     r"[^,;.!?]{0,40}?\bbefore\b[^,;.!?]{0,40}?\b%s\b" % _LAND, True),
    (r"\b%s\s+(?:be\s+)?(?:fix\w*|resolv\w*|address\w*|correct\w*|repair\w*|remediat\w*)\b"
     r"[^,;.!?]{0,40}?\b(?:first|before)\b" % _ORDERED, True),
    (r"\b(?:needs?|requires?|awaits?|awaiting|pending)\s+(?:\w+\s+){0,2}?(?:revisions?|rework|changes|fixes|corrections?)\b", True),
    # F4 -- another round, or the work handed back to its author
    (r"\b(?:another|a\s+further|an\s+additional|one\s+more|a\s+second|further)\s+"
     r"(?:review\s+)?(?:rounds?|revisions?|passes|pass|iterations?|reviews?|attempts?|cycles?)\b", True),
    (r"\b%s\s+%s\s+back\b" % (_HANDBACK, _UNDER_REVIEW), False),
    (r"\b%s\s+%s\s+(?:back\s+)?to\s+(?:the\s+)?%s\b" % (_HANDBACK, _UNDER_REVIEW, _AUTHOR), False),
    (r"\breturn\w*\s+(?:%s\s+)?for\s+(?:rework|revisions?|changes|fixes|another)\b" % _UNDER_REVIEW, False),
    (r"\bback\s+to\s+(?:the\s+)?(?:%s|drawing\s+board)\b" % _AUTHOR, False),
    # F5 -- a rejection whose object is the work under review
    (r"\breject(?:s|ed|ing)?\s+(?:(?:this|that|the|your|its|their|my|our)\s+)?(?:\w+\s+){0,2}?%s\b" % _WORK, False),
    (r"\b(?:this|that|the|your)\s+(?:\w+\s+){0,2}?%s\s+"
     r"(?:is|was|are|were|has\s+been|have\s+been|had\s+been)\s+rejected\b" % _WORK, False),
    (r"\breject(?:s|ed|ing)?\s+(?:this|it)\s*(?:[,;.!?]|$|\b(?:because|since|due|owing|given)\b)", True),
    (r"\b(?:the\s+)?reviewer\s+rejects?\b", False),
    (r"\bverdict\b\W{0,8}reject(?:ed)?\b", False),
    # Fixed governance tokens, which are identifiers rather than prose guesses
    (r"changes[ _-]required|do not merge|needs? (?:more|further) work|"
     r"不應(?:該)?合併|尚未完成|還沒(?:有)?完成", False),
))
# Contrastive conjunctions. A resolution after one of these is about the
# other side of the contrast, never about the thing just asserted. This is
# a grammatical role, not a vocabulary of rejections: it constrains where a
# cancellation may be read from, and cannot create one.
_CONTRAST = re.compile(
    r"\b(?:although|though|even\s+though|even\s+if|but|whereas|while|however|nevertheless|"
    r"nonetheless|despite|in\s+spite\s+of|notwithstanding|yet|aside\s+from|other\s+than|"
    r"apart\s+from|besides)\b",
    re.I
)

_CONTRARY_TOKEN = re.compile(r"\bREJECT(?:ED)?\b|\bCHANGES[ _]REQUIRED\b|\bDO NOT MERGE\b")


# -- decision shape -------------------------------------------------------------
#
# Everything above this line is *vocabulary*: which English sentences mean
# "no". Round 6's independent review showed that a correctly bound PASS plus
# ``Current decision: reject`` still completed, and the response is deliberately
# not another row in _REJECTION_RULES. Widening the vocabulary is the move that
# failed in Rounds 2, 3 and 4, and it fails for a structural reason: the set of
# sentences meaning "no" has no edge, so coverage can always be measured and
# will always be short.
#
# What does have an edge is the *shape a decision is written in*. A reviewer
# announcing a decision writes a decision field ("Verdict:", "Current
# decision:") or a first-person decision verb ("I reject this"). That is a small
# closed form, and the value in it is read against a small closed vocabulary --
# so this asks "did you state a decision, and can I read it?", never "does this
# sentence sound negative?".
#
# Unreadable is not silence. A decision field ADM cannot map is a conflict, for
# the same reason an unknown verdict in the object is INVALID rather than
# absent: the one thing an unreadable decision does not establish is that the
# reviewer agreed.
#
# Two label tiers, because fail-closed has a cost on the other side. A DECISIVE
# label is unambiguously announcing a decision, so an unreadable value there is
# a conflict. A REPORTING label ("Result:", "Conclusion:") is ordinary review
# prose that *may* carry a decision, so it counts only when its value is itself
# an unambiguous rejection -- otherwise "Conclusion: the fix is correct" would
# withdraw a genuine approval, and a gate nothing can pass is just a wall.
_DECISIVE_LABELS = (
    "verdict", "decision", "review decision", "reviewer decision", "review verdict",
    "current decision", "final decision", "final verdict", "final call", "final review",
    "outcome", "review outcome", "approval", "sign off", "signoff", "disposition",
    "recommendation", "overall decision", "overall verdict", "my decision", "my verdict",
    "review result", "决定", "決定", "裁決", "裁决", "審查結論", "审查结论",
)
# ...and, since Round 7, the finite GRAMMAR those strings were samples of.
#
# Round 6 built the copula form as a grammar -- an owner, an optional modifier,
# a decision noun -- and left the label form as the tuple above. So the same
# decision, written with a colon instead of a verb, fell outside the contract:
# "My decision is to reject" was read and "My current decision: reject" was not.
# Grok 4.6 named six such fields, and a bound PASS beside them completed.
#
# The answer is not six more strings. A tuple closes exactly what somebody
# thought to list, and the next reviewer writes the seventh -- that is how
# Rounds 2, 3 and 4 were lost, one wording at a time. What a decision field
# actually is, is a small closed composition:
#
#     (owner)? (modifier)* (decision-noun)
#
# Every part is a closed set, so the set of accepted fields is finite and
# stated rather than discovered. NOTHING here touches _REJECT_VALUES: this
# widens what counts as the *field*, never what counts as a rejection.
_LABEL_OWNERS = frozenset({"my", "our", "the", "its", "their"})
_LABEL_MODIFIERS = frozenset({"current", "final", "overall", "review", "reviewer",
                              "official", "formal", "considered"})
# Only nouns whose sense IS an announced decision. "result", "status" and
# "conclusion" are reporting words that may or may not carry one, so they stay
# in the reporting tier below and are deliberately not promoted by a modifier:
# "Final result: 3 passed" is a count, and reading it as an unreadable decision
# would stall an honest review.
_DECISION_NOUNS = frozenset({"decision", "verdict", "recommendation", "disposition", "outcome"})
_REPORTING_LABELS = (
    "result", "status", "conclusion", "review conclusion", "assessment", "review status",
    "final status", "judgement", "judgment", "call", "overall", "summary verdict", "結論", "结论",
)
_LABEL_LINE = re.compile(
    # A leading ">" is admitted for the same reason the withdrawal region is
    # widened rather than narrowed: a decision line stays a decision line when
    # it is quoted, and excluding quotes would make "> Verdict: reject" a
    # one-character bypass. Reading it can only cost a round.
    r"^\s{0,3}(?:>\s?)*\s{0,3}(?:[-*•]\s*)?(?:#{1,6}\s*)?(?:\*\*|__|\*|`)?\s*"
    r"(?P<label>[A-Za-z][A-Za-z \-]{0,24}|[一-鿿]{2,6})"
    r"\s*(?:\*\*|__|\*|`)?\s*[:：]\s*(?P<value>.*?)\s*$"
)
# The same decision field written as a sentence. "My decision is to reject" is
# the label form with a copula instead of a colon, and "My review verdict is
# PASS" -- a genuine approval from the Round-5 corpus -- is the same shape, which
# is what makes it a shape rather than a rejection pattern.
_COPULA_DECISION = re.compile(
    # Built from the same closed sets as the label form above. Round 6 wrote
    # these alternations out by hand and the two forms drifted apart: neither
    # knew "reviewer" or "official" as a modifier, and the label form had no
    # grammar at all. Drifting apart is precisely what R6-IR-1 was.
    r"\b(?:%s)\s+(?:(?:%s)\s+){0,2}"
    r"(?P<noun>decision|verdict|recommendation|disposition|outcome|call|assessment|conclusion|judgement|judgment)\s+"
    r"(?:is|was|are|were|remains?|stands?\s+as|will\s+be)\s+(?:to\s+)?(?P<value>[^.;!?\n]{1,40})"
    % ("|".join(sorted(_LABEL_OWNERS)), "|".join(sorted(_LABEL_MODIFIERS))),
    re.I
)
# Nouns whose sentence form is unambiguously announcing a decision; the rest get
# the reporting tier's treatment, exactly as their label form does.
_DECISIVE_NOUNS = _DECISION_NOUNS

_DECISIVE, _REPORTING = "decisive", "reporting"


def _label_tier(label):
    """``decisive`` / ``reporting`` / ``None`` for the text before a decision colon.

    The explicit tables are consulted first, so every Round-5 and Round-6 label
    keeps exactly the tier it had -- both the decisive ones the grammar would
    not generate ("final call", "approval", "sign off", the Chinese labels) and
    the reporting ones it must not ("final status", "summary verdict").

    Only then is the composition tried, and only for the decisive tier. A
    grammar that could invent *reporting* labels would be inventing tolerance,
    and tolerance is the direction that has to be earned rather than guessed.
    """
    if label in _DECISIVE_LABELS:
        return _DECISIVE
    if label in _REPORTING_LABELS:
        return _REPORTING
    words = label.split()
    if words and words[0] in _LABEL_OWNERS:
        words = words[1:]
    if words and words[-1] in _DECISION_NOUNS and all(w in _LABEL_MODIFIERS for w in words[:-1]):
        # "my current decision", "the overall disposition", "final outcome".
        # "the recommendation engine" is not one of them: its head noun is
        # "engine", which is how an ordinary noun phrase stays ordinary.
        return _DECISIVE
    return None

# First person, because "I reject this" carries no colon and is exactly as
# explicit as "Decision: reject". The object has to be the work under review;
# without that, "I cannot approve the budget for another reviewer" would count.
_FIRST_PERSON = re.compile(
    r"\b(?:i|we)\s+(?P<neg>%s\s+(?:\w+\s+){0,2})?(?P<verb>approv\w*|accept\w*|sign\s*-?\s*off|reject\w*|"
    r"declin\w*|withhold\w*|am\s+rejecting|am\s+not\s+approving)\b"
    r"(?P<tail>[^.;!?\n]{0,40})" % _NOT,
    re.I
)
# The review's own outcome predicated of the work under review: "This fails my
# review", "the patch did not pass review". Still shape rather than sentiment --
# the subject must be the work, the predicate must be pass/fail, and the object
# must be the review itself -- which is why "the tests fail" does not match it.
_REVIEW_OUTCOME = re.compile(
    r"\b(?:this|it|that|(?:this|that|the|your)\s+(?:\w+\s+){0,1}?%s)\s+"
    r"(?P<neg>%s\s+)?(?P<verb>fail\w*|pass\w*|clear\w*|did\s+not\s+pass|does\s+not\s+pass)\s+"
    r"(?:my|our|the|this)\s+(?:\w+\s+){0,1}?(?:review|verdict|approval|assessment)\b" % (_WORK, _NOT),
    re.I
)

_APPROVE_VALUES = frozenset({
    "pass", "passed", "passes", "passing", "approve", "approved", "approval", "accept",
    "accepted", "acceptable", "ok", "okay", "lgtm", "yes", "green", "clean", "complete",
    "completed", "done", "granted", "sign off", "signed off", "signoff", "go", "ship",
    "ship it", "merge", "no objections", "no findings", "none", "satisfied", "positive",
    "通過", "通过", "同意", "核准",
})
_REJECT_VALUES = frozenset({
    "reject", "rejected", "rejects", "rejecting", "fail", "failed", "fails", "failing",
    "failure", "no", "nack", "block", "blocked", "blocking", "withheld", "withhold",
    "denied", "deny", "refused", "negative", "rework", "revise", "revision required",
    "revisions required", "changes required", "changes requested", "changes needed",
    "needs changes", "needs work", "needs revision", "needs rework", "not approved",
    "not approve", "do not approve", "do not merge", "do not complete", "do not ship",
    "not ready", "not acceptable", "unacceptable", "no go", "nogo", "incomplete",
    "more work required", "another round", "changes", "駁回", "驳回", "拒絕", "拒绝",
    "不通過", "不通过", "退回",
})
_VALUE_TRIM = re.compile(r"^[\s\-–—*_`\"'“”‘’()\[\]{}]+|[\s\-–—*_`\"'“”‘’()\[\]{}.,;:!?。，；：！？]+$")


def _decision_value(raw):
    """``reject`` / ``approve`` / ``unreadable`` for the value of a decision field."""
    token = _VALUE_TRIM.sub("", raw or "")
    token = re.sub(r"\s+", " ", token).strip().lower().replace("_", " ").replace("-", " ")
    token = re.sub(r"\s+", " ", token)
    if not token:
        return contracts.STATEMENT_UNREADABLE  # "Decision:" with nothing after it
    if token in _REJECT_VALUES:
        return contracts.STATEMENT_REJECT
    if token in _APPROVE_VALUES:
        return contracts.STATEMENT_APPROVE
    return contracts.STATEMENT_UNREADABLE


def decision_statements(text):
    """Every decision the reviewer announced in prose: ``[{raw, polarity}]``.

    Shape, not sentiment. Only a decision field or a first-person decision verb
    is read, and the value is mapped against a closed vocabulary -- an
    unmappable value in a decisive field is ``unreadable``, which
    ``contracts.review_authority`` treats as a conflict rather than as silence.
    """
    statements = []
    for line in (text or "").split("\n"):
        match = _LABEL_LINE.match(line)
        if not match:
            continue
        label = re.sub(r"\s+", " ", match.group("label")).strip().lower().replace("-", " ")
        polarity = _decision_value(match.group("value"))
        tier = _label_tier(label)
        if tier == _DECISIVE:
            statements.append({"raw": line.strip()[:120], "polarity": polarity})
        elif tier == _REPORTING and polarity == contracts.STATEMENT_REJECT:
            # A reporting label counts only on an unambiguous rejection; its
            # unreadable values are ordinary prose, not a withheld decision.
            statements.append({"raw": line.strip()[:120], "polarity": polarity})
    for match in _COPULA_DECISION.finditer(text or ""):
        polarity = _decision_value(match.group("value"))
        decisive = match.group("noun").lower() in _DECISIVE_NOUNS
        if decisive or polarity == contracts.STATEMENT_REJECT:
            statements.append({"raw": match.group(0).strip()[:120], "polarity": polarity})
    for match in _REVIEW_OUTCOME.finditer(text or ""):
        failing = bool(re.match(r"fail|did\s+not\s+pass|does\s+not\s+pass", match.group("verb"), re.I))
        if bool(match.group("neg")) != failing:      # "fails" / "does not pass" -> reject
            polarity = contracts.STATEMENT_REJECT    # "passes" / "did not fail"  -> approve
        else:
            polarity = contracts.STATEMENT_APPROVE
        statements.append({"raw": match.group(0).strip()[:120], "polarity": polarity})
    for clause in _CLAUSE_SPLIT.split(text or ""):
        for match in _FIRST_PERSON.finditer(clause):
            verb = match.group("verb").lower()
            tail = match.group("tail") or ""
            negated = bool(match.group("neg"))
            positive = bool(re.match(r"approv|accept|sign", verb))
            if positive and not negated:
                polarity = contracts.STATEMENT_APPROVE
            elif positive:
                polarity = contracts.STATEMENT_REJECT  # "I do not approve ..."
            elif negated:
                continue  # "I do not reject this" is not a decision shape worth reading
            else:
                polarity = contracts.STATEMENT_REJECT
            # The decision has to be about the work under review, not about
            # something else the reviewer happens to reject.
            if not re.match(r"\s*(?:$|[,;.!?]|(?:of|on|for|upon)?\s*(?:this|it|that|the\s+%s|your\s+%s|"
                            r"(?:this|the|your)\s+\w+\s+%s)\b)" % (_WORK, _WORK, _WORK), tail, re.I):
                continue
            statements.append({"raw": match.group(0).strip()[:120], "polarity": polarity})
    return statements


def _negated(clause, match):
    """Is this match cancelled -- by negation, by resolution, or by attribution?

    Round 3 looked only at the words *before* the match and at the match's first
    word. That missed negation inside the matched proposition ("The blocker no
    longer exists" matched blocker->exists and withdrew a genuine approval), and
    it missed negation in the predicate just after a short match ("A blocker no
    longer exists", where the rule matches only "A blocker"). Polarity is read
    over the window before, the whole matched span, and the first words after it.
    """
    before = _WORD.findall(clause[:match.start()])[-_NEG_WINDOW:]
    inside = _WORD.findall(match.group(0))
    after = _WORD.findall(clause[match.end():])[:_PREDICATE_WINDOW]
    if any(_NEGATOR.match(word) for word in before + inside + after):
        return True
    if re.search(r"\b%s\b" % _HISTORICAL, clause[:match.start()], re.I):
        return True
    return _resolved_after(clause, match)


def _resolved_after(clause, match):
    """Does the clause say this condition has since been dealt with?

    "The issue that blocked merge is now fixed" names a blocker and clears it in
    the same breath. An ordered fix is not a resolution, so "a blocker remains
    and must be fixed" still counts as a rejection.

    A resolution introduced by a *contrastive* conjunction is about something
    else -- that is what contrast is for. "A blocker remains although the
    timeout is fixed" resolves the timeout, not the blocker, and Codex found
    that this construction cancelled a live blocker and completed the task. The
    tail is therefore cut at the first contrast word before looking for a
    resolution.

    This is the withdrawal net, not the gate. It can only ever *remove* a claim,
    so a miss here costs a round rather than a task, and the contract's own
    answer to "a blocker remains" is a blocking entry in the decision's
    ``findings`` -- which needs no reading of prose at all.
    """
    tail = clause[match.end():]
    contrast = _CONTRAST.search(tail)
    if contrast:
        tail = tail[:contrast.start()]
    for found in re.finditer(r"\b%s\b" % _RESOLVED, tail, re.I):
        head = tail[:found.start()]
        if not re.search(r"\b%s\b[\w\s]{0,20}$" % _ORDERED, head, re.I):
            return True
    return False


def _approval_rejected(clause):
    """An approval noun whose state is negative -- with the state's own polarity.

    A single regex could not do this: "Approval is not conditional" contains both
    an approval and a negative-sounding predicate, yet it is an approval. The
    state is what carries the meaning, and the state can itself be negated.
    """
    noun = re.search(r"\b%s\b" % _APPROVAL_NOUN, clause, re.I)
    if not noun:
        return None
    tail = clause[noun.end():]
    for state in re.finditer(r"\b%s\b" % _APPROVAL_STATE, tail, re.I):
        if re.search(r"\b(?:not|never|no\s+longer|nor)\s+(?:\w+\s+){0,2}$", tail[:state.start()], re.I):
            continue  # "not conditional", "no longer blocked" -- the state is denied
        return clause[noun.start():noun.end() + state.end()].strip()[:120]
    for absent in re.finditer(r"\b%s\b" % _APPROVAL_ABSENT, tail, re.I):
        after = tail[absent.end():]
        if re.match(r"\s*(?:\w+\s+){0,2}?%s\b" % _APPROVAL_STATE, after, re.I):
            continue  # "not conditional", "never blocked" -- the state is denied
        return clause[noun.start():noun.end() + absent.end()].strip()[:120]
    return None



def rejection_signal(text):
    """The structural rejection a message states in its own prose, or None."""
    for clause in _CLAUSE_SPLIT.split(text or ""):
        clause = clause.strip()
        if not clause:
            continue
        approval = _approval_rejected(clause)
        if approval:
            return approval
        for pattern, guarded in _REJECTION_RULES:
            for match in pattern.finditer(clause):
                if not guarded or not _negated(clause, match):
                    return match.group(0).strip()[:120]
    return None


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _same(field, left, right):
    if r.FIELD_KINDS[field] == "sha" and isinstance(left, str) and isinstance(right, str):
        return left.startswith(right) or right.startswith(left)
    return left == right


def _longer(left, right):
    return left if isinstance(left, str) and isinstance(right, str) and len(left) >= len(right) else right


# -- source 1/2: payloads -----------------------------------------------------------------

def _stream_json(content):
    """(text, native_payload, warnings) from Claude-style stream-json."""
    last = None
    for line in content.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            last = event
    if last is None:
        return "", None, ["stream-json: no result event"]
    warnings = ["stream-json: provider reported is_error"] if last.get("is_error") else []
    native = last.get("structured_output")
    text = last.get("result") if isinstance(last.get("result"), str) else ""
    return text, native if isinstance(native, dict) else None, warnings


def _fenced_blocks(lines):
    """[(lang, body, closed, line_indexes)] for every ```adm-result / ```json block."""
    return [region for region in _fence_regions(lines) if region[0] in _PAYLOAD_LANGS]


def _fenced_payload(lines, signals, fenced_lines):
    """The single agreed payload, or None. ``attempted`` says whether one was tried.

    Every line of every structured block is added to ``fenced_lines`` so the
    prose tiers never re-read payload text as if it were narrative.
    """
    payloads, attempted = [], False
    for lang, body, closed, indexes in _fenced_blocks(lines):
        ours = lang == "adm-result" or r.REPORT_SCHEMA_VERSION in body
        if not ours:
            continue
        fenced_lines.update(indexes)
        attempted = True
        if not closed:
            signals.add("extract.payload_truncated")
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            signals.add("extract.payload_invalid_json")
            continue
        if not isinstance(payload, dict):
            signals.add("extract.payload_invalid_json")
            continue
        payloads.append(payload)
    distinct = {json.dumps(p, sort_keys=True) for p in payloads}
    if len(distinct) > 1:
        signals.add("extract.conflicting_statements")
        return None, attempted
    return (payloads[-1] if payloads else None), attempted


def _fenced_decisions(lines, fenced_lines, warnings):
    """Every structured reviewer decision in the message, exactly as written.

    Nothing is judged here: a block is collected whether or not it is valid,
    bound, or even parseable. Dropping an unreadable decision at extraction
    would recreate the Round-4 defect one layer down -- an unparseable decision
    would silently become no decision, and a PASS elsewhere would stand. The
    invalid block is carried forward so contracts.review_authority can block on
    it explicitly.

    The *channel* is narrow, and Round 6 narrowed it: only a ```adm-review-result
    fence is authoritative. Round 5 also collected any fence whose body merely
    contained the schema string, so ```json, ```yaml, ```text, ```markdown and
    even a bare ``` fence all carried authority -- and a fence is exactly the
    place a quoted example, a documentation snippet or a pasted transcript
    lives. A decision in the wrong fence is not collected and not blocked; it is
    reported back as ignored, because silently dropping it would leave a
    compliant-looking reviewer wondering why nothing happened, while *blocking*
    on it would hand anyone a way to stall a task by quoting JSON.
    """
    decisions = []
    for lang, body, closed, indexes in _fence_regions(lines):
        if lang != _DECISION_LANG:
            if contracts.REVIEW_SCHEMA in body:
                warnings.append(
                    f"review decision: ignored, a decision must be in a ```{_DECISION_LANG} fence, "
                    f"not ```{lang or '(none)'}")
            continue
        fenced_lines.update(indexes)
        if not closed:
            decisions.append({"schema": contracts.REVIEW_SCHEMA, "unreadable": "the decision block was truncated"})
            warnings.append("review decision: the block was never closed")
            continue
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            decisions.append({"schema": contracts.REVIEW_SCHEMA, "unreadable": f"invalid JSON: {exc}"})
            warnings.append("review decision: the block is not valid JSON")
            continue
        for block in (parsed if isinstance(parsed, list) else [parsed]):
            if isinstance(block, dict):
                decisions.append(block)
            else:
                decisions.append({"schema": contracts.REVIEW_SCHEMA,
                                  "unreadable": f"decision is a {type(block).__name__}, not an object"})
    return decisions


def _payload_facts(payload, tier, signals, warnings, role):
    facts, lists = {}, {"evidence": [], "blockers": [], "warnings": [], "findings": []}
    for problem in r.report_problems(payload):
        warnings.append(problem)
    for field in r.FACT_FIELDS:
        if field in REVIEW_AUTHORITY_FIELDS and role != v.REVIEWER:
            # Only a reviewer can assert a verdict. A worker payload carrying
            # one is dropped here as well as in the deterministic tier, so the
            # rule does not depend on which tier the claim arrived through.
            if payload.get(field) is not None:
                warnings.append(f"{field}: ignored, only a reviewer may assert it")
            continue
        if field.startswith("ssot_sync_"):
            sync = payload.get("ssot_sync_status")
            raw = sync.get(field[len("ssot_sync_"):]) if isinstance(sync, dict) else None
        else:
            raw = payload.get(field)
        if raw is None:
            continue
        if _FIELD_VALIDATORS[field].is_valid(raw):
            facts[field] = raw
        else:
            warnings.append(f"{field}: invalid reported value dropped")
    for key in ("task_id", "status"):
        if key not in facts:
            signals.add("result.required_field_missing")
    for key in ("blockers", "warnings", "findings"):
        items = payload.get(key)
        if isinstance(items, list):
            lists[key] = [item for item in items if isinstance(item, str)]
    evidence = payload.get("evidence")
    if isinstance(evidence, list):
        lists["evidence"] = [item for item in evidence
                             if isinstance(item, dict) and isinstance(item.get("kind"), str) and isinstance(item.get("ref"), str)]
    return facts, lists


# -- source 3: deterministic lines ---------------------------------------------------------------

def test_counts(text):
    """Public: parse "N passed, N failed, N skipped" style counts out of text.

    Shared with manager.nextplan.verify so a test summary recorded by ADM is
    read by exactly the same parser as one an agent quotes. Requires runner-specific
    anchored structure (pytest summary or unittest block) to reject non-test prose,
    and ignores fenced / quoted / documentation regions (see non_evidence_lines).
    """
    return _counts(text)


def _unittest_counts(lines, index):
    """Counts for a unittest block whose OK/FAILED line is ``lines[index]``."""
    status = _UNITTEST_STATUS.match(lines[index].strip())
    if not status:
        return None
    cursor = index - 1
    while cursor >= 0 and not lines[cursor].strip():
        cursor -= 1
    ran = _UNITTEST_RAN.match(lines[cursor].strip()) if cursor >= 0 else None
    if not ran:
        return None
    total, detail = int(ran.group(1)), status.group("bad")
    skipped = 0
    m_skip = re.search(r"skipped=(\d+)", status.group("ok") or detail or "")
    if m_skip:
        skipped = int(m_skip.group(1))
    if detail is None:
        return {"tests_passed": total - skipped, "tests_failed": 0, "tests_skipped": skipped}
    failed = sum(int(n) for n in re.findall(r"(?:failures|errors)=(\d+)", detail))
    return {"tests_passed": max(0, total - failed - skipped), "tests_failed": failed, "tests_skipped": skipped}


def _pytest_counts(stripped):
    """Counts for one pytest summary line, or None if the line is not one."""
    match = (_PYTEST_EQUAL_LINE.match(stripped) or
             _PYTEST_TIMED_LINE.match(stripped) or
             _PYTEST_COMMA_LINE.match(stripped))
    if not match:
        return None
    found = {}
    for number, word in _COUNT.findall(match.group("counts")):
        word = "errors" if word.startswith("error") else word
        found[word] = found.get(word, 0) + int(number)
    if not found:
        return None
    counts = {"tests_passed": found.get("passed", 0),
              "tests_failed": found.get("failed", 0) + found.get("errors", 0)}
    if "skipped" in found:
        counts["tests_skipped"] = found["skipped"]
    return counts


def _counts(text, regions=True):
    """The counts of the *last* runner summary in ``text``, or {}.

    Two rules, both of which an earlier round got wrong:

    - documentation is not evidence: fenced, quoted and example lines are
      dropped before anything is parsed;
    - the last valid summary wins, for unittest exactly as for pytest. The
      unittest block used to be searched top-down across the whole text and
      returned first, so a quoted "Ran 99 tests ... OK" shadowed the real
      "0 passed" -- and a real *failure* -- printed below it.
    """
    if not text or not isinstance(text, str):
        return {}
    source = text.replace("\r\n", "\n").split("\n")
    skip = non_evidence_lines(source) if regions else set()
    kept = [line for index, line in enumerate(source) if index not in skip]
    for index in range(len(kept) - 1, -1, -1):
        counts = _pytest_counts(kept[index].strip())
        if counts is None:
            counts = _unittest_counts(kept, index)
        if counts is not None:
            return counts
    return {}


# Formatting an agent wraps a value in, and the punctuation it ends a sentence
# with, are not part of the value. Stripping them once was not enough:
# "**rejected**." lost its leading stars, kept the trailing ones behind the
# period, and missed the enum -- so the verdict stayed UNKNOWN and a payload
# PASS survived next to a written rejection. Strip to a fixpoint instead, with
# a cap so no input can loop, and keep the mapping itself exact: the loop makes
# the *token* robust, it never widens what counts as a verdict.
_WRAPPERS = "*`_~\"'“”‘’「」『』()[]{}<>【】"
_TRAILING = ".:;,!?。：；，！？、… \t"
_NORMALIZE_CAP = 8


def _normalize_token(raw):
    """``(token, exhausted)``. ``exhausted`` means the cap was hit before the
    token stopped changing, so what came back is NOT a settled reading of it."""
    token = raw.strip()
    for _ in range(_NORMALIZE_CAP):
        stripped = token.strip().strip(_WRAPPERS).strip(_TRAILING).strip()
        if stripped == token:
            return token, False
        token = stripped
    return token, True



def _map_value(field, raw, role):
    token, exhausted = _normalize_token(raw)
    if exhausted:
        # Wrapping deep enough to outlast the cap leaves the value unread.
        # Returning the truncated token would let an unreadable verdict pass
        # for silence, which is how a contradictory payload PASS survived.
        return None
    upper = token.upper().replace(" ", "_").replace("-", "_")
    # Exact matches only. A prefix fallback used to collapse a hedge into a
    # clean verdict -- "PASS_WITH_CAVEATS" became PASS and
    # "APPROVED_WITH_COMMENTS" became a passing review. A qualified answer is
    # not the unqualified one; unmapped words fall through to a warning and
    # stay UNKNOWN.
    if field == "status":
        return _STATUS_WORDS.get(upper)
    if field == "review_verdict":
        return _VERDICT_WORDS.get(upper)
    if field in ("commit_sha", "head_sha", "remote_sha", "base_sha", "reviewed_sha"):
        match = _HEX.search(token.lower())
        return match.group(0) if match else None
    if field == "branch":
        match = re.search(r"[A-Za-z0-9._/-]+", token)
        return match.group(0) if match else None
    if field == "push_status":
        lowered = token.lower()
        if re.search(r"not pushed|未\s*push|未推送|no push", lowered):
            return "not_pushed"
        if re.search(r"fail|失敗|rejected|denied", lowered):
            return "failed"
        if re.search(r"pushed|已\s*push|已推送|synced|success", lowered):
            return "pushed"
        return None
    if field == "git_status":
        lowered = token.lower()
        if re.search(r"\bdirty\b|uncommitted|未提交|有變更", lowered):
            return "dirty"
        if re.search(r"\bclean\b|乾淨|nothing to commit", lowered):
            return "clean"
        return None
    if field == "ssot_sync_drive":
        lowered = token.lower()
        if re.search(r"not required|n/a|不需要", lowered):
            return "not_required"
        if re.search(r"fail|失敗|unavailable|error", lowered):
            return "failed"
        if re.search(r"synced|已同步|read-?back (?:ok|pass|verified)", lowered):
            return "synced"
        return None
    if field == "progress_percent":
        match = re.search(r"(\d{1,3})\s*%", token)
        return int(match.group(1)) if match and int(match.group(1)) <= 100 else None
    return None


def _deterministic(lines, role, warnings, skip, signals=None):
    """({field: [values]}, set of consumed line indexes, blockers). Lines in ``skip`` are payload."""
    seen, consumed, blockers = {}, set(skip), []
    signals = signals if signals is not None else set()

    def add(field, value, index):
        if value is None:
            return
        seen.setdefault(field, [])
        if not any(_same(field, value, prior) for prior in seen[field]):
            seen[field].append(value)
        consumed.add(index)

    for index, line in enumerate(lines):
        if index in skip:
            continue
        stripped = line.strip()
        if (_PYTEST_EQUAL_LINE.match(stripped) or
                _PYTEST_TIMED_LINE.match(stripped) or
                _PYTEST_COMMA_LINE.match(stripped)):
            for field, value in _counts(stripped).items():
                add(field, value, index)
            continue
        ran = _UNITTEST_RAN.match(stripped)
        if ran:
            add("tests_run", int(ran.group(1)), index)
            continue
        failed = _UNITTEST_FAILED.match(stripped)
        if failed:
            numbers = [int(n) for n in re.findall(r"(?:failures|errors)=(\d+)", failed.group(1))]
            add("tests_failed", sum(numbers), index)
            continue
        match = _KV_LINE.match(line)
        if not match:
            continue
        field = _KEY_ALIASES.get(match.group("key").strip().lower())
        raw = match.group("val")
        if field is None or not raw.strip():
            continue
        if field == "tests":
            counts = _counts(raw)
            for name, value in counts.items():
                add(name, value, index)
            if counts:
                continue
            if re.search(r"not run|未執行|沒有跑|no tests", raw, re.I):
                add("tests_run", 0, index)
            continue
        if field == "blockers":
            consumed.add(index)
            if not _NONE_WORDS.match(raw.strip()):
                blockers.append(raw.strip())
            continue
        if field == "review_verdict" and role != v.REVIEWER:
            continue
        mapped = _map_value(field, raw, role)
        if mapped is None:
            warnings.append(f"deterministic: unmapped {field} value {raw.strip()[:60]!r}")
            if field in REVIEW_AUTHORITY_FIELDS:
                # The reviewer stated a verdict and it could not be read. That
                # is an unresolved decision, not the absence of one, so it must
                # not leave a payload PASS standing unopposed.
                signals.add("extract.conflicting_statements")
                consumed.add(index)
            continue
        add(field, mapped, index)
    return seen, consumed, blockers


# -- source 4: constrained heuristics -------------------------------------------------------------

def _heuristic_status(lines, consumed):
    text = "\n".join(line for index, line in enumerate(lines) if index not in consumed)
    blocked, done, failed = (bool(p.search(text)) for p in (_BLOCKED_PROSE, _DONE_PROSE, _FAIL_PROSE))
    if blocked:
        return "BLOCKED"
    if done and not failed:
        return "PASS"
    if failed and not done:
        return "FAIL"
    return None


# -- entry point ---------------------------------------------------------------------------------------

def extract(output):
    """Normalize one agent output envelope.

    ``output``: ``{event_id, task_id, role, format ('text'|'json'|'stream-json'),
    content, session_id?}``. ``task_id``/``role``/``session_id`` come from ADM's
    own dispatch record, never from the agent.
    """
    content = (output.get("content") or "").replace("\r\n", "\n")
    role = output["role"]
    signals, warnings = set(), []
    facts, lists = {}, {"evidence": [], "blockers": [], "warnings": [], "findings": []}
    tier = "none"

    text, native = content, None
    fmt = output.get("format", "text")
    if fmt == "stream-json":
        text, native, stream_warnings = _stream_json(content)
        warnings.extend(stream_warnings)
    elif fmt == "json":
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = None
            signals.add("extract.payload_invalid_json")
        if isinstance(parsed, dict) and parsed.get("schema_version", "").startswith("adm-ai-result"):
            native, text = parsed, ""
        elif isinstance(parsed, dict) and parsed.get("schema") == contracts.REVIEW_SCHEMA:
            # A reviewer whose whole output is the decision object. Without this
            # the block fell into the branch below and was discarded, which is
            # fail-closed but makes a compliant reviewer look silent.
            native, text = parsed, ""
        elif parsed is not None:
            warnings.append("json output is not an adm-ai-result report")
            text = ""

    lines = text.split("\n") if text else []
    fenced_lines = set()
    # Collected before the payload so a decision block's lines are never also
    # read as prose or as a result report.
    decisions = _fenced_decisions(lines, fenced_lines, warnings)
    if native is not None and native.get("schema") == contracts.REVIEW_SCHEMA:
        decisions.append(native)
        native = None
    if decisions and role != v.REVIEWER:
        # Only a reviewer holds review authority. A worker message carrying a
        # decision block is dropped here, exactly as a worker payload carrying
        # review_verdict is, so authority never depends on which channel a claim
        # arrived through.
        warnings.append(f"review decision: ignored, {role} may not assert one")
        signals.add("result.required_field_missing")
        decisions = []
    payload, attempted = (native, True) if native is not None else _fenced_payload(lines, signals, fenced_lines)
    if native is None and fmt == "json" and "extract.payload_invalid_json" in signals:
        attempted = True
    if payload is not None:
        tier = "native" if native is not None else "fenced"
        facts, lists = _payload_facts(payload, tier, signals, warnings, role)

    sources = {field: f"agent:{tier}" for field in facts}
    # Evidence narrows: a fenced, quoted or documented line may not prove anything.
    non_evidence = non_evidence_lines(lines)
    deterministic, consumed, blockers = _deterministic(
        lines, role, warnings, fenced_lines | non_evidence, signals)
    for field, values in deterministic.items():
        candidate = values[0]
        if len(values) > 1:
            signals.add("extract.conflicting_statements")
            facts.pop(field, None)
            sources[field] = f"conflict:{values!r}"
            continue
        if field in facts:
            if _same(field, facts[field], candidate):
                facts[field] = _longer(facts[field], candidate)
                continue
            signals.add("extract.conflicting_statements")
            sources[field] = f"conflict: payload={facts.pop(field)!r} text={candidate!r}"
            continue
        if field in sources:  # already a conflict
            continue
        facts[field] = candidate
        sources[field] = "agent:deterministic"
        if tier in ("none", "heuristic"):
            tier = "deterministic"
    if blockers and not lists["blockers"]:
        lists["blockers"] = blockers

    result = r.blank_result(output["event_id"], output["task_id"], role, raw_sha256=_sha256(content))
    for field, value in facts.items():
        result["facts"][field] = r.fact(value, v.REPORTED, sources[field])
    for field, source in sources.items():
        if source.startswith("conflict"):
            result["facts"][field] = r.unknown(source[:300])

    # tests_run is arithmetic over reported counts when not stated: DERIVED.
    if r.level(result, "tests_run") == v.UNKNOWN and "tests_run" not in sources:
        passed, failed = r.value(result, "tests_passed"), r.value(result, "tests_failed")
        if r.level(result, "tests_passed") == v.REPORTED and r.level(result, "tests_failed") == v.REPORTED:
            result["facts"]["tests_run"] = r.fact(passed + failed, v.DERIVED, "derived:test_count_sum")

    # A reviewer's PASS must be something the reviewer actually said.
    #
    # Round 3 let a fenced payload carry review_verdict PASS on its own, and then
    # tried to take it back whenever the prose looked like a rejection. That is
    # fail-open by construction: it treats "no rejection matched" as "the reviewer
    # approved", so every wording the matcher missed became an approval. Codex
    # completed 26 of 30 held-out rejections that way -- and a payload with no
    # prose at all completed too.
    #
    # The direction is now reversed. A PASS needs an explicit authoritative
    # decision line ("Verdict: PASS", "Final review: APPROVED") that survived
    # region filtering, mapped exactly onto the enum, and did not conflict. A
    # payload may still carry the value, but it cannot authorize it alone. FAIL
    # is deliberately NOT gated: a payload asserting failure only ever routes
    # away from completion, so it needs no anchor.
    if role == v.REVIEWER and r.value(result, "review_verdict") == "PASS":
        stated = deterministic.get("review_verdict") or []
        if stated != ["PASS"]:
            result["facts"]["review_verdict"] = r.unknown(
                "payload claimed PASS but the reviewer stated no explicit verdict of its own")
            warnings.append("review_verdict: a payload PASS alone cannot authorize a review")


    # Prose the agent wrote outside any payload or parsed line.
    prose = "\n".join(line for index, line in enumerate(lines) if index not in consumed)
    # Withdrawal widens: everything outside the agent's own payload counts,
    # including the regions evidence may not be read from. A rule that can only
    # remove a claim is safe almost everywhere -- ``unfenced`` below is the one
    # place Round 7 had to draw a line, and only for the reviewer's decision.
    # ``written`` still feeds the quota / rate-limit heuristics, which read what
    # happened to the run rather than what anyone decided about it.
    claimed_lines = consumed - fenced_lines - non_evidence
    written = "\n".join(line for index, line in enumerate(lines)
                        if index not in fenced_lines and index not in claimed_lines)

    # The reviewer's own VOICE: ``written``, minus every fenced region.
    #
    # Round 3 kept fenced text inside the withdrawal net, because back then a
    # payload ``review_verdict: PASS`` was a live claim and narrowing the net to
    # unfenced prose would have handed anyone a bypass -- write the rejection in
    # a fence and the payload's PASS stands. Round 5 removed that claim (a
    # payload can no longer authorize anything) and Round 6 narrowed authority to
    # one fence, so the bypass the net was defending has no way in any more.
    #
    # What was left was only its cost, and Grok 4.6 measured it: a bound PASS
    # beside an ordinary ```json fence quoting ``"verdict": "REJECT"`` -- a
    # schema example, a transcript, another tool's output -- read as a
    # contradiction and stalled the task, 5 of 5 fence languages. TRUST-MODEL
    # 6.4 says in writing that a decision in the wrong fence neither authorizes
    # NOR blocks; Round 6 implemented only the first half.
    #
    # So a fence is quotation or data in BOTH directions. This is the channel
    # rule, not a weakening of withdrawal: the reviewer's real decision has a
    # channel of its own, and unfenced prose still withdraws exactly as before.
    # CLOSED fences only. An unclosed fence is not a quotation -- it is a
    # malformed one, and its "body" runs to the end of the message. Excluding
    # that would let a reviewer swallow its own rejection by opening a fence and
    # never closing it, which is a bypass this rule would otherwise have
    # created. Reading it costs a round at worst; not reading it costs a
    # completion.
    quoted = set()
    for _lang, _body, closed, indexes in _fence_regions(lines):
        if closed:
            quoted.update(indexes)
    unfenced = "\n".join(line for index, line in enumerate(lines)
                          if index not in quoted and index not in claimed_lines)

    # A positive claim is withdrawn when the same message states the opposite in
    # its own words: a payload saying review_verdict PASS beside a written
    # rejection is a contradiction, not an approval. This can only ever remove a
    # claim, which is why a heuristic is allowed to do it.
    # Decisions the reviewer announced in prose. Carried on the result rather
    # than folded into a fact, because they are not a claim ADM reasons over:
    # contracts.review_authority consults them, and only to withdraw. They are
    # read from the reviewer's own voice: prose outside every fence. Round 7
    # moved both this and the withdrawal check off ``written`` and onto
    # ``unfenced``; see the note above that region for why a fence is now
    # quotation in both directions.
    if role == v.REVIEWER:
        result["decision_statements"] = decision_statements(unfenced)

    token = _CONTRARY_TOKEN.search(unfenced)
    contrary = rejection_signal(unfenced) or (token.group(0) if token else None)
    if contrary:
        for field in ("status", "review_verdict"):
            if r.value(result, field) == "PASS" and r.level(result, field) in (v.REPORTED, v.DERIVED):
                result["facts"][field] = r.unknown(
                    f"claimed PASS, but the same message states the opposite in prose: {contrary!r}"[:300])
                signals.add("extract.conflicting_statements")

    # Negative heuristics read only what the agent said about its own run:
    # prose outside payloads, plus declared blockers. Payload findings or code
    # that merely *mention* rate limiting must not trip them.
    heuristic_text = "\n".join([written if payload is None else "", *lists["blockers"]])
    if _QUOTA.search(heuristic_text):
        signals.add("result.quota_exhausted")
    elif _RATE.search(heuristic_text):
        signals.add("result.rate_limited")
    if (not attempted and r.level(result, "status") == v.UNKNOWN and "status" not in sources):
        status = _heuristic_status(lines, consumed)
        if status is not None:
            result["facts"]["status"] = r.fact(status, v.DERIVED, "agent:heuristic")
            if tier == "none":
                tier = "heuristic"
    if tier == "none" and signals & {"result.quota_exhausted", "result.rate_limited"}:
        tier = "heuristic"
    if payload is None and tier in ("none", "heuristic"):
        signals.add("extract.no_parseable_payload")

    reported_task = r.value(result, "task_id")
    reported_session = r.value(result, "session_id")
    if reported_task is not None and reported_task != output["task_id"]:
        signals.add("result.identity_mismatch")
    if output.get("session_id") and reported_session is not None and reported_session != output["session_id"]:
        signals.add("result.identity_mismatch")

    result["extraction"].update(tier=tier, signals=sorted(signals), warnings=warnings)
    for key, items in lists.items():
        result[key] = items
    result["decisions"] = decisions
    return r.validate_result(result)
