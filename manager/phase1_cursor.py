"""Durable, crash-safe Phase-1 cursor primitive for actual-invocation fair scheduling.

Three invariants govern every durable write here:

* **Monotonicity.** The persisted ``generation`` never decreases. It is
  derived from the generation actually found on disk under a verified
  compare-and-swap, never from the generation a caller happens to carry
  in memory.
* **Coverage.** A write never drops per-project state that the durable
  cursor already held. A caller that knows about one project cannot
  erase the other twelve.
* **One winner.** Concurrent writers presenting the same generation do
  not both succeed. Read, compare and replace are one critical section
  over one path resolved exactly once.

All three used to be defeatable, by four separate routes:

1. ``expected_generation=None`` -- and equally, omitting the argument --
   skipped the CAS outright.
2. The new generation was computed as ``cursor_data["generation"] + 1``,
   so even a *correct* CAS token did not help: a caller holding a stale
   in-memory snapshot (generation 5) replaced a generation-2458 file
   with a generation-6 one while passing the CAS.
3. The loader turned *any* exception -- and a missing file -- into a
   default generation-0 state, and the CAS re-read through that same
   defaulting loader, so ``0 == 0`` passed and the next write collapsed
   the file to generation 1 carrying only the projects that one
   invocation happened to see.
4. Compare and ``os.replace`` were not serialized, so two writers could
   both read generation N, both pass the CAS, and both write N+1.

Each is closed below, and each has a named regression test in
``manager/test_phase1_cursor_integrity.py``.
"""

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from manager.manager_home import ManagerHomeError, resolve_manager_home


class PhaseCursorError(Exception):
    """Base for every durable Phase-1 cursor failure."""
    pass


class StaleCursorError(PhaseCursorError):
    """Raised when a concurrent/stale writer attempts to overwrite a newer cursor generation."""
    pass


class CursorContractError(PhaseCursorError):
    """Raised when a caller does not present a valid CAS intent.

    Misuse of the API, not a state problem: no ``expected_generation`` at
    all, or ``None`` (which no longer means anything -- it used to mean
    "bypass the CAS").
    """
    pass


class CursorStateError(PhaseCursorError):
    """Base for durable state that exists but cannot be trusted. Always fails closed."""
    pass


class CursorReadError(CursorStateError):
    """The cursor file exists but could not be read (permissions, I/O, lock)."""
    pass


class CursorParseError(CursorStateError):
    """The cursor file exists but is not valid JSON."""
    pass


class CursorSchemaError(CursorStateError):
    """The cursor file parses but violates the cursor schema."""
    pass


class CursorMissingError(PhaseCursorError):
    """No durable cursor exists.

    Deliberately NOT a :class:`CursorStateError`: "absent" is a
    legitimate first-ever-boot state, while "present but untrustworthy"
    never is. Callers that can legitimately bootstrap catch this one
    specifically; nothing may catch it by catching corruption.
    """
    pass


class CursorLockError(PhaseCursorError):
    """The per-cursor mutation lock could not be acquired in time.

    Fails the write closed. A writer that cannot take the lock has not
    established what the durable generation is, so it has no basis on
    which to replace anything.
    """
    pass


class _CreateOnly:
    """Sentinel: this write may only create a cursor that does not exist yet."""

    __slots__ = ()

    def __repr__(self):
        return "CREATE_ONLY"


CREATE_ONLY = _CreateOnly()


class _Unset:
    """Sentinel distinguishing "argument omitted" from any value a caller could pass."""

    __slots__ = ()

    def __repr__(self):
        return "<unset>"


_UNSET = _Unset()

# How long a writer waits for the per-cursor mutation lock before failing
# closed. Generous relative to the work under the lock (one small read,
# one small write), so reaching it means something is genuinely wrong
# rather than merely contended.
CURSOR_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.01


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resolve_cursor_path(manager_home=None, cursor_path=None):
    """Locate the durable cursor, or raise ManagerHomeError -- never cwd.

    The old default here read AI_MANAGER_HOME with the working directory
    as its fallback, so it wrote ``runtime/phase1-cursor.json`` into
    whatever directory the process happened to start in.  When that
    directory was the activated production checkout it dirtied the tree
    and fail-closed every Scheduled Task (2026-09-02 outage).  Resolution
    now goes through the single canonical resolver, which fails closed.

    Every public entry point below calls this exactly once and then
    passes the resulting :class:`Path` down. Nothing re-resolves mid
    operation, so a manager home that changes underneath a running
    mutation cannot make it read one file and replace another.
    """
    if cursor_path is not None:
        return Path(cursor_path)
    return resolve_manager_home(manager_home) / "runtime" / "phase1-cursor.json"


def default_phase1_cursor():
    """The empty cursor state, as an explicit request.

    This is the ONLY sanctioned way to obtain a default: a caller asks
    for it because it knows it is bootstrapping. It is never handed back
    silently in place of state that failed to load.
    """
    return {
        "project_cursor": 0,
        "per_project_record_cursor": {},
        "per_project_attention_visits": {},
        "generation": 0,
        "updated_at": None,
    }


# Retained under the old private name for the existing call sites that use it.
_default_cursor = default_phase1_cursor


# --------------------------------------------------------------------------
# Bound-path primitives. Everything below takes an already-resolved Path and
# never resolves one itself -- that is what makes "resolved once per
# operation" a property of the module rather than a habit of its callers.
# --------------------------------------------------------------------------


def _exists_at(path):
    try:
        return path.exists()
    except OSError as exc:
        raise CursorReadError(f"{path}: cannot stat durable cursor: {exc}") from exc


def _require_int(data, key, label):
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CursorSchemaError(f"{label}: {key!r} must be a non-negative int, found {value!r}")
    return value


def _require_counter_map(data, key, label):
    """A project_id -> non-negative int map. Absent is fine; malformed is not.

    Absent means a legacy file written before the field existed. Present
    but wrong means the file is not what it claims to be -- and silently
    dropping the bad entries would be exactly the coverage loss this
    module exists to prevent.
    """
    if key not in data or data.get(key) is None:
        return {}
    value = data.get(key)
    if not isinstance(value, dict):
        raise CursorSchemaError(f"{label}: {key!r} must be an object, found {type(value).__name__}")
    clean = {}
    for name, count in value.items():
        if not isinstance(name, str):
            raise CursorSchemaError(f"{label}: {key!r} has a non-string project id {name!r}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise CursorSchemaError(f"{label}: {key!r}[{name!r}] must be a non-negative int, found {count!r}")
        clean[name] = count
    return clean


def _load_at(path, missing_ok):
    """Strict load from an already-bound path. The only reader of cursor bytes."""
    label = str(path)

    if not _exists_at(path):
        if missing_ok:
            return default_phase1_cursor()
        raise CursorMissingError(f"{label}: no durable cursor exists")

    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise CursorParseError(f"{label}: durable cursor is not valid UTF-8: {exc}") from exc
    except OSError as exc:
        raise CursorReadError(f"{label}: cannot read durable cursor: {exc}") from exc

    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise CursorParseError(f"{label}: durable cursor is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise CursorSchemaError(f"{label}: durable cursor must be an object, found {type(data).__name__}")

    updated_at = data.get("updated_at")
    if updated_at is not None and not isinstance(updated_at, str):
        raise CursorSchemaError(f"{label}: 'updated_at' must be a string or null, found {updated_at!r}")

    return {
        "project_cursor": _require_int(data, "project_cursor", label),
        "per_project_record_cursor": _require_counter_map(data, "per_project_record_cursor", label),
        "per_project_attention_visits": _require_counter_map(data, "per_project_attention_visits", label),
        "generation": _require_int(data, "generation", label),
        "updated_at": updated_at,
    }


def _clean_counter_map(value):
    if not isinstance(value, dict):
        return {}
    return {str(k): int(v) for k, v in value.items()
            if not isinstance(v, bool) and isinstance(v, (int, float)) and v >= 0}


# One threading.Lock per resolved cursor path, so that threads within a
# process serialize even where the OS file lock is held per-process rather
# than per-handle. The file lock below covers separate processes; this
# covers threads. Neither is a second *authority* over the cursor -- the
# durable generation remains the only source of truth, and both locks only
# decide who gets to look at it first.
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS = {}


def _process_lock_for(path):
    key = os.path.normcase(os.path.abspath(str(path)))
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _try_lock_fd(fd):
    """Non-blocking exclusive lock on one byte of the lock file. True if taken."""
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
    if msvcrt is not None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    try:
        import fcntl
    except ImportError:
        # No advisory-locking facility at all. The in-process lock still
        # holds, and the CAS still refuses a lower generation, so this
        # degrades to "correct but not serialized" rather than unsafe.
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock_fd(fd):
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
    if msvcrt is not None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    try:
        import fcntl
    except ImportError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def _cursor_mutation_lock(path, timeout=None):
    """Serialize read-compare-replace for one bound cursor path.

    A sidecar ``.lock`` file, never the cursor itself: locking the cursor
    would mean holding an open handle to the very file ``os.replace``
    swaps out. The OS drops the lock when the holder exits, so a crashed
    writer cannot wedge the runtime -- which is why this is an OS lock
    and not a lock file whose staleness someone has to adjudicate.
    """
    if timeout is None:
        timeout = CURSOR_LOCK_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout

    process_lock = _process_lock_for(path)
    if not process_lock.acquire(timeout=max(timeout, 0.0)):
        raise CursorLockError(f"{path}: timed out after {timeout}s waiting for the in-process cursor lock")
    try:
        lock_path = path.with_name(path.name + ".lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise CursorLockError(f"{lock_path}: cannot open the cursor mutation lock: {exc}") from exc
        try:
            while True:
                if _try_lock_fd(fd):
                    break
                if time.monotonic() >= deadline:
                    raise CursorLockError(
                        f"{lock_path}: timed out after {timeout}s waiting for the cursor mutation lock"
                    )
                time.sleep(_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                _unlock_fd(fd)
        finally:
            os.close(fd)
    finally:
        process_lock.release()


def _replace_atomically(path, payload):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name
    try:
        os.replace(temp_name, str(path))
    except Exception:
        if os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def phase1_cursor_exists(manager_home=None, cursor_path=None):
    """Whether a durable cursor file is present.

    Advisory only -- it answers "should this caller present CREATE_ONLY
    or a generation?", and :func:`save_phase1_cursor` re-verifies the
    answer authoritatively under the lock. A file that appears or
    vanishes between the two therefore costs a rejected write, never a
    corrupted one.
    """
    return _exists_at(_resolve_cursor_path(manager_home=manager_home, cursor_path=cursor_path))


def load_phase1_cursor(manager_home=None, cursor_path=None, missing_ok=True):
    """Load the durable Phase-1 cursor, failing closed on anything untrustworthy.

    The four outcomes are distinct, because conflating them is how a
    2458-generation cursor becomes a generation-0 one:

    * absent -- :class:`CursorMissingError`, or the default state when
      ``missing_ok`` (the one place a default is legitimate);
    * unreadable -- :class:`CursorReadError`;
    * unparseable -- :class:`CursorParseError`;
    * parseable but not a valid cursor -- :class:`CursorSchemaError`.

    ``missing_ok`` covers only *absence*. No value of it will turn a
    corrupt or unreadable cursor into a default one; that is the whole
    point of the split.
    """
    return _load_at(_resolve_cursor_path(manager_home=manager_home, cursor_path=cursor_path),
                    missing_ok=missing_ok)


def save_phase1_cursor(cursor_data, manager_home=None, cursor_path=None,
                       expected_generation=_UNSET, lock_timeout=None):
    """Atomically persist the Phase-1 cursor under a mandatory compare-and-swap.

    ``expected_generation`` is required and has exactly two meanings:

    * :data:`CREATE_ONLY` -- create a cursor that does not exist yet.
      Fails if one does. The created cursor is generation 1.
    * a non-negative ``int`` -- update an existing cursor whose durable
      generation is exactly that. Fails if the cursor is absent, or if
      the durable generation differs.

    There is no third meaning and no way to opt out: ``None`` is refused,
    and omitting the argument is refused. The new generation is
    ``durable_generation + 1``, read from disk inside the CAS -- what the
    caller believes the generation to be is never consulted, so a stale
    in-memory snapshot cannot roll the file backward even when it
    presents the right CAS token.

    Per-project maps are merged onto the durable state rather than
    replacing it: a caller that only knows about one project advances
    that project and preserves the rest.

    The path is resolved once, at the top, and the read, the compare, the
    final re-verification and the replace all use that one bound path,
    inside the per-cursor mutation lock.
    """
    if expected_generation is _UNSET:
        raise CursorContractError(
            "save_phase1_cursor() requires expected_generation: pass CREATE_ONLY to create a "
            "cursor that does not exist yet, or the durable generation this write is based on."
        )
    if expected_generation is None:
        raise CursorContractError(
            "expected_generation=None is refused: it used to mean 'skip the CAS', which let a "
            "stale writer replace a newer cursor. Pass CREATE_ONLY or a non-negative int."
        )
    creating = expected_generation is CREATE_ONLY
    if not creating and (isinstance(expected_generation, bool) or not isinstance(expected_generation, int)
                         or expected_generation < 0):
        raise CursorContractError(
            f"expected_generation must be CREATE_ONLY or a non-negative int, found {expected_generation!r}"
        )

    # Resolved exactly once. Everything below is bound to `path`.
    path = _resolve_cursor_path(manager_home=manager_home, cursor_path=cursor_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with _cursor_mutation_lock(path, timeout=lock_timeout):
        # The compare half of the compare-and-swap. `missing_ok=False`, so an
        # absent file raises instead of presenting generation 0 -- the whole
        # point is that "there is nothing here" and "here is a generation-0
        # cursor" must not be the same answer.
        if creating:
            if _exists_at(path):
                raise StaleCursorError(
                    f"{path}: CREATE_ONLY write refused, a durable cursor already exists"
                )
            durable = None
            base_generation = 0
        else:
            try:
                durable = _load_at(path, missing_ok=False)
            except CursorMissingError as exc:
                raise StaleCursorError(
                    f"{path}: write expected generation {expected_generation} but no durable cursor exists; "
                    "pass CREATE_ONLY to create one"
                ) from exc
            base_generation = durable["generation"]
            if base_generation != expected_generation:
                raise StaleCursorError(
                    f"Cursor generation mismatch: expected {expected_generation}, found {base_generation}"
                )

        # Authoritative: durable state + 1, never the caller's snapshot.
        new_generation = base_generation + 1
        project_cursor = int(cursor_data.get("project_cursor", 0))
        if project_cursor < 0:
            project_cursor = 0

        # Merge, never replace: per-project state the durable cursor holds and
        # this snapshot does not mention is carried forward. Nothing in the
        # runtime deletes a project's cursor entry, so there is no deletion
        # contract to preserve here; adding one would need its own explicit
        # verb rather than an omission from a partial snapshot.
        record_cursors = dict(durable["per_project_record_cursor"]) if durable else {}
        record_cursors.update(_clean_counter_map(cursor_data.get("per_project_record_cursor", {})))
        attention_visits = dict(durable["per_project_attention_visits"]) if durable else {}
        attention_visits.update(_clean_counter_map(cursor_data.get("per_project_attention_visits", {})))

        payload = {
            "project_cursor": project_cursor,
            "per_project_record_cursor": record_cursors,
            "per_project_attention_visits": attention_visits,
            "generation": new_generation,
            "updated_at": now_iso(),
        }

        # Re-verify the same bound path immediately before the swap. Under the
        # lock nothing legitimate can have moved underneath us, so a change
        # here means something outside the contract is writing the cursor --
        # exactly the case that must not be papered over by replacing it.
        if creating:
            if _exists_at(path):
                raise StaleCursorError(
                    f"{path}: CREATE_ONLY write refused, a durable cursor appeared during the write"
                )
        else:
            try:
                verify = _load_at(path, missing_ok=False)
            except CursorMissingError as exc:
                raise StaleCursorError(
                    f"{path}: durable cursor disappeared during the write; refusing to recreate it"
                ) from exc
            if verify["generation"] != base_generation:
                raise StaleCursorError(
                    f"Cursor generation changed during the write: based on {base_generation}, "
                    f"found {verify['generation']}"
                )

        _replace_atomically(path, payload)

    return payload
