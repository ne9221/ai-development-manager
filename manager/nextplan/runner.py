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

This is deliberately *not* wired into ``manager/repo_write_enforcement.py``.
Round 5 builds the evidence path and the gate that consumes it; activating it in
the live write path is a separate, reviewed change.
"""

from __future__ import annotations

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


def run_validation(argv, cwd, runner="pytest", timeout_seconds=DEFAULT_TIMEOUT_SECONDS, spawn=subprocess.run):
    """Spawn ``argv`` and return one ``adm-validation-result/v1``.

    ``argv`` must be a list. Passing a shell string raises rather than quietly
    degrading: a string is the shape that cannot carry provenance, and accepting
    one "just this once" is how the old path stayed exploitable.
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

    execution_id = f"exec-{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="adm-validation-") as workspace:
        report = Path(workspace) / "report.xml"
        # -p no:cacheprovider keeps the run from writing into the checkout.
        full_argv = [*argv, f"--junitxml={report}", "-p", "no:cacheprovider"]
        record = {
            "schema": VALIDATION_SCHEMA, "execution_id": execution_id, "argv": full_argv,
            "runner": runner, "started": False, "exit_code": None,
            "tests": None, "timed_out": False, "output_summary": "",
        }
        try:
            completed = spawn(full_argv, cwd=str(cwd), shell=False, text=True, encoding="utf-8",
                              errors="replace", capture_output=True, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
            record.update(started=True, timed_out=True, output_summary=output[-MAX_OUTPUT_CHARS:])
            return record
        except (OSError, ValueError) as exc:
            # The process never started. `started: False` makes the record
            # invalid for evidence, which is the correct reading: nothing ran.
            record.update(output_summary=f"validation runner could not be started: {exc}"[-MAX_OUTPUT_CHARS:])
            return record
        output = (completed.stdout or "") + (completed.stderr or "")
        record.update(started=True, exit_code=completed.returncode,
                      output_summary=output[-MAX_OUTPUT_CHARS:],
                      tests=parse_junit_xml(report))
        return record


def pytest_argv(targets=(), extra=(), executable=None):
    """The argv for a pytest run of ``targets``.

    ``sys.executable`` rather than a bare name, for the same reason
    repo_write_enforcement resolves one: the interpreter must be the
    already-authoritative one, not whatever the ambient PATH resolves to.
    """
    return [executable or sys.executable, "-m", "pytest", *[str(t) for t in targets], *[str(e) for e in extra]]
