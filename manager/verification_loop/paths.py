"""Repo-relative path canonicalisation for risk and impact matching.

Phase B-1 matched diff paths against risk rules with a raw ``startswith``.
The B-1 independent review reproduced three evasions of the production risk
rule with that: ``./manager/execution_runner.py``, a backslash spelling, and a
different case all slipped past a ``manager/`` prefix and the run was ACCEPTED.
Everything here exists to make a rule impossible to dodge by re-spelling a
path, so canonicalisation happens once, before any matching.

Two deliberate decisions worth naming, because both trade convenience for
fail-closed behaviour:

* **Matching is case-insensitive.** The repo's first-class runtime target is
  Windows, where ``Manager/Foo.py`` and ``manager/foo.py`` are the same file.
  Matching case-sensitively would let a rule be evaded by changing case on the
  platform ADM actually runs on.
* **An uncanonicalisable path is not silently skipped.** Absolute paths, drive
  letters and ``..`` traversal return None rather than some best-effort
  string. A caller that cannot canonicalise a diff path cannot prove which
  rules apply to it, so the evaluator turns that into UNKNOWN rather than
  letting an unmatched path quietly escalate nothing.
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

# A Windows drive-letter prefix ("C:", "c:/", "C:\\"). Checked separately from
# the leading-separator test because "C:foo" is drive-relative, not absolute,
# yet is just as much "not repo-relative" as "C:\foo".
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def canonical_repo_path(raw: object) -> Optional[str]:
    """Canonical repo-relative form of ``raw``, or None if it is not one.

    Returns None -- never a guess -- for anything that is not a plain
    repo-relative path: a non-string, the empty string, an absolute path, a
    Windows drive-letter path, a UNC path, or any path containing a ``..``
    segment. None is the caller's signal to fail closed.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    text = text.replace("\\", "/")

    # Reject anything anchored outside the repo before normalising, so a
    # rejected shape can never be normalised into an accepted one.
    if text.startswith("/") or _DRIVE_PREFIX.match(text):
        return None

    segments = []
    for segment in text.split("/"):
        if segment in ("", "."):
            # Collapses "a//b", "./a" and a trailing "a/" in one place.
            continue
        if segment == "..":
            # Never resolved against the preceding segment: "manager/../x" is
            # rejected outright rather than becoming "x". Resolving it would
            # mean a diff path could be written to *look* like it touches a
            # guarded directory while landing somewhere else, or vice versa.
            return None
        segments.append(segment)

    if not segments:
        return None
    return "/".join(segments)


def _match_key(canonical: str) -> str:
    return canonical.casefold()


def path_matches_prefix(path: object, prefix: object) -> bool:
    """Whether ``path`` is ``prefix`` itself or lies beneath it.

    Both sides are canonicalised first, and the comparison is anchored to a
    segment boundary: prefix ``manager`` matches ``manager/foo.py`` but not
    ``manager-extra/foo.py``. A rule whose own prefix cannot be canonicalised
    matches nothing rather than everything.
    """
    canonical_path = canonical_repo_path(path)
    canonical_prefix = canonical_repo_path(prefix)
    if canonical_path is None or canonical_prefix is None:
        return False
    key_path = _match_key(canonical_path)
    key_prefix = _match_key(canonical_prefix)
    return key_path == key_prefix or key_path.startswith(key_prefix + "/")


def canonicalise_all(paths: Sequence[object]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split ``paths`` into (canonical, rejected-verbatim).

    The rejected side is returned rather than dropped so the caller can report
    *which* path it could not reason about instead of silently narrowing the
    rule surface.
    """
    canonical: list[str] = []
    rejected: list[str] = []
    for raw in paths:
        value = canonical_repo_path(raw)
        if value is None:
            rejected.append(raw if isinstance(raw, str) else repr(raw))
        else:
            canonical.append(value)
    return tuple(canonical), tuple(rejected)
