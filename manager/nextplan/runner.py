"""Runner adapters: test counts that belong to one process ADM actually spawned.

Round 4's independent review ran this, for real, through ADM's own validation
path::

    echo documentation; pytest & echo ===== 12 passed in 3.10s =====

Only the two ``echo`` calls executed. The exit code was 0. ADM recorded
``VERIFIED tests_run=12`` and completed the task. Two separate mistakes had to
line up, and both are structural rather than lexical:

1. **Designation read the command string.** ``is_test_command`` looked for the
   *word* ``pytest`` among a shell string's tokens, so a printed argument was
   indistinguishable from an executed program. Splitting on ``&`` as well as
   ``;`` would have fixed this one line and left the shape intact.
2. **Counts came from the whole command's stdout.** Even with perfect
   designation, ``pytest && echo "===== 999 passed ====="`` attributes the echo
   to pytest, because nothing tied the numbers to a process.

So this module does not parse stdout at all. An adapter

* takes an **argv list** and spawns it with ``shell=False`` -- there is no shell
  to chain a second command onto, so "the program that ran" is a fact rather
  than a reading;
* asks the runner to write its own **structured report** (pytest's JUnit XML)
  to a path ADM chose, and takes the counts from that file.

``echo`` cannot write a JUnit XML. Neither can a documentation transcript, a
help page, or any amount of convincing text. The counts exist only if a test
runner produced them, and they are stamped with the ``execution_id`` of the run
that produced them, so they cannot later be attributed to a different execution.

Round 6 closes what that left open. Round 5 built a *producer* that could not be
fooled and a *consumer* that never checked whether the producer had run: the
consumer validated the record's shape, saw an ``execution_id`` and an ``argv``,
and believed the counts. Grok's independent review typed this by hand::

    {"schema": "adm-validation-result/v1", "execution_id": "exec-forged",
     "argv": ["python", "-m", "pytest"], "started": true, "exit_code": 0,
     "tests": {"passed": 999, "failed": 0, "skipped": 0}}

and obtained ``VERIFIED tests_run=999`` and MARK_COMPLETE, without anything
being spawned. Shape had been made to stand in for provenance, which is the
Round-2 defect in a JSON costume.

So an execution now has to be one ADM itself issued. ``ExecutionRegistry`` mints
the ``execution_id`` *before* the spawn, binds it to the task and run that asked
for it, and records what the adapter observed: the argv digest, the artifact
path and the digest of the artifact bytes the adapter read. A returned record is
only a *reference* to one of those entries -- **the counts are read from the
registry, never from the record** -- so fabricating numbers achieves nothing:
they are not consulted. Fabricating an ``execution_id`` achieves nothing either,
because ADM never issued it, and with no entry on file there are no counts at
all.

This is deliberately *not* wired into ``manager/repo_write_enforcement.py``.
Rounds 5 and 6 build the evidence path and the gate that consumes it; activating
it in the live write path is a separate, reviewed change.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from manager.nextplan.contracts import VALIDATION_SCHEMA

DEFAULT_TIMEOUT_SECONDS = 900
MAX_OUTPUT_CHARS = 20000

# The programs each adapter is willing to be. Checked against argv[0] (and the
# module after ``-m``) so a result cannot claim ``runner: "pytest"`` for a
# process that was never pytest.
_PROGRAMS = {"pytest": ("pytest", "py.test")}
_INTERPRETERS = re.compile(r"^(?:python[\d.]*|py|pypy[\d.]*)$")


class ValidationRunError(ValueError):
    """The caller asked for something that cannot produce bound evidence."""


def argv_digest(argv):
    """A stable digest of one argument vector."""
    return hashlib.sha256("\x00".join(str(token) for token in argv).encode("utf-8")).hexdigest()


def _file_digest(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


class ExecutionRegistry:
    """ADM's own record of the validation runs it started.

    This is the thing a worker cannot write into. Every entry is minted by
    :meth:`issue` *before* a process exists, so an ``execution_id`` is a
    capability ADM handed out rather than a string an agent chose; and every
    entry is completed by the adapter that did the spawning, from what it
    observed rather than from what anyone reported.

    Deliberately small, and deliberately not a service. It is a plain dict of
    records, so ADM can keep it inside the task/run state it already persists --
    the security property comes from *who writes it*, not from where it lives.
    A registry that outlived its task would only widen the window in which a
    stale ``execution_id`` is still honoured.
    """

    def __init__(self, task_id=None, run_id=None, records=None):
        self.task_id = task_id
        self.run_id = run_id
        self.records = dict(records or {})

    def issue(self, argv, task_id=None, run_id=None):
        """Mint an execution identity for ``argv``, before anything is spawned."""
        execution_id = f"exec-{uuid.uuid4().hex}"
        self.records[execution_id] = {
            "execution_id": execution_id,
            "task_id": task_id if task_id is not None else self.task_id,
            "run_id": run_id if run_id is not None else self.run_id,
            "argv_sha256": argv_digest(argv),
            "nonce": uuid.uuid4().hex,
            "issued_by_adm": True,
            "started": False, "exit_code": None, "timed_out": False,
            "artifact_path": None, "artifact_sha256": None, "counts": None,
        }
        return self.records[execution_id]

    def complete(self, execution_id, **observed):
        """Record what the adapter observed. Only known fields are writable."""
        record = self.records[execution_id]
        for field in ("started", "exit_code", "timed_out", "artifact_path", "artifact_sha256", "counts"):
            if field in observed:
                record[field] = observed[field]
        return record

    def lookup(self, execution_id):
        return self.records.get(execution_id) if isinstance(execution_id, str) else None


def _program_of(argv):
    """The program argv actually invokes, seeing through ``python -m pkg``."""
    head = os.path.basename(str(argv[0])).lower()
    head = re.sub(r"\.(exe|cmd|bat|ps1)$", "", head)
    if _INTERPRETERS.match(head):
        rest = list(argv[1:])
        while rest:
            token = str(rest[0])
            if token == "-m" and len(rest) > 1:
                return str(rest[1]).split(".")[0].lower()
            if token.startswith("-"):
                rest.pop(0)
                continue
            break
        return head
    return head


def parse_junit_xml(path):
    """``{passed, failed, skipped}`` from a JUnit report the runner wrote, or None.

    The runner is the author of this file; ADM only chose where it goes. A
    report that is absent, empty or unparseable yields no counts at all, which
    leaves the completion proof failing -- never passing by omission.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.strip():
        return None
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        return None
    total = failures = errors = skipped = 0
    for suite in suites:
        try:
            total += int(suite.get("tests", 0))
            failures += int(suite.get("failures", 0))
            errors += int(suite.get("errors", 0))
            skipped += int(suite.get("skipped", 0))
        except (TypeError, ValueError):
            return None
    passed = total - failures - errors - skipped
    if passed < 0:
        return None  # an incoherent report is not evidence
    return {"passed": passed, "failed": failures + errors, "skipped": skipped}


def run_validation(argv, cwd, runner="pytest", timeout_seconds=DEFAULT_TIMEOUT_SECONDS, spawn=subprocess.run,
                   registry=None, task_id=None, run_id=None):
    """Spawn ``argv`` and return one ``adm-validation-result/v1``.

    ``argv`` must be a list. Passing a shell string raises rather than quietly
    degrading: a string is the shape that cannot carry provenance, and accepting
    one "just this once" is how the old path stayed exploitable.

    ``registry`` is ADM's :class:`ExecutionRegistry`. When one is given the
    execution identity is minted from it *before* the spawn and completed from
    what this function observed, and that entry -- not the returned record -- is
    what any consumer is allowed to read counts from. Calling without a registry
    still runs the process honestly; the result simply has nothing to bind to
    and can never become VERIFIED evidence.
    """
    if isinstance(argv, str) or not isinstance(argv, (list, tuple)) or not argv:
        raise ValidationRunError("argv must be a non-empty list; a shell command string cannot be bound to a process")
    argv = [str(token) for token in argv]
    accepted = _PROGRAMS.get(runner)
    if accepted is None:
        raise ValidationRunError(f"no runner adapter for {runner!r}")
    program = _program_of(argv)
    if program not in accepted:
        raise ValidationRunError(f"argv runs {program!r}, which is not {runner!r}")

    with tempfile.TemporaryDirectory(prefix="adm-validation-") as workspace:
        # A nonce in the filename, in a directory this call created: the report
        # ADM reads cannot be a file that was lying there beforehand, and the
        # pre-existence check below is cheap enough to keep as a hard assertion
        # rather than an assumption about tempfile.
        report = Path(workspace) / f"report-{uuid.uuid4().hex}.xml"
        if report.exists():
            raise ValidationRunError("the validation report path already exists; refusing to read a pre-existing artifact")
        # -p no:cacheprovider keeps the run from writing into the checkout.
        full_argv = [*argv, f"--junitxml={report}", "-p", "no:cacheprovider"]
        entry = (registry.issue(full_argv, task_id=task_id, run_id=run_id) if registry is not None
                 else {"execution_id": f"exec-{uuid.uuid4().hex}"})
        execution_id = entry["execution_id"]
        record = {
            "schema": VALIDATION_SCHEMA, "execution_id": execution_id, "argv": full_argv,
            "runner": runner, "started": False, "exit_code": None,
            "tests": None, "timed_out": False, "output_summary": "",
        }

        def finish(**observed):
            record.update({k: x for k, x in observed.items() if k in record})
            if registry is not None:
                registry.complete(execution_id, started=record["started"], exit_code=record["exit_code"],
                                  timed_out=record["timed_out"], artifact_path=str(report),
                                  artifact_sha256=observed.get("artifact_sha256"),
                                  counts=record["tests"])
            return record

        try:
            completed = spawn(full_argv, cwd=str(cwd), shell=False, text=True, encoding="utf-8",
                              errors="replace", capture_output=True, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
            return finish(started=True, timed_out=True, output_summary=output[-MAX_OUTPUT_CHARS:])
        except (OSError, ValueError) as exc:
            # The process never started. `started: False` makes the record
            # invalid for evidence, which is the correct reading: nothing ran.
            return finish(output_summary=f"validation runner could not be started: {exc}"[-MAX_OUTPUT_CHARS:])
        output = (completed.stdout or "") + (completed.stderr or "")
        return finish(started=True, exit_code=completed.returncode,
                      output_summary=output[-MAX_OUTPUT_CHARS:],
                      tests=parse_junit_xml(report), artifact_sha256=_file_digest(report))


def pytest_argv(targets=(), extra=(), executable=None):
    """The argv for a pytest run of ``targets``.

    ``sys.executable`` rather than a bare name, for the same reason
    repo_write_enforcement resolves one: the interpreter must be the
    already-authoritative one, not whatever the ambient PATH resolves to.
    """
    return [executable or sys.executable, "-m", "pytest", *[str(t) for t in targets], *[str(e) for e in extra]]
