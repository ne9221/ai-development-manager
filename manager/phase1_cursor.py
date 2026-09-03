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
  not both succeed. Read, compare and publish are one critical section
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

On top of those, a mutation is a small **custody transaction** with
deterministic recovery semantics (see "Recovery protocol" below). Every
combination of the durable artifacts beside the cursor -- the cursor
itself, the initialization-state record, and any custody claims -- has
exactly one meaning, and the only one that ever creates a generation-1
cursor is a *proven* first initialization.
"""

import errno
import hashlib
import json
import os
import threading
import time
import uuid
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


class CursorInitStateError(CursorStateError):
    """The initialization-state record exists but is not a valid record.

    A zero-byte, truncated, mis-schema'd or mis-addressed record is
    neither "initialized" nor "never initialized": it is untrustworthy,
    and the only safe reading of untrustworthy provenance is to refuse to
    mutate until a human has looked.
    """
    pass


class CursorMissingError(PhaseCursorError):
    """No durable cursor exists.

    Deliberately NOT a :class:`CursorStateError`: "absent" is a
    legitimate first-ever-boot state, while "present but untrustworthy"
    never is. Callers that can legitimately bootstrap catch this one
    specifically; nothing may catch it by catching corruption.
    """
    pass


class CursorRecoveryRequiredError(PhaseCursorError):
    """The durable artifacts say "not a first boot", but there is no live cursor to amend.

    Deliberately NOT a :class:`CursorStateError` and NOT a
    :class:`CursorMissingError`: it is neither ordinary corruption nor a
    legitimate first boot, and nothing may catch it as either. It is the
    one condition the runtime cannot resolve on its own, because every
    automatic resolution available to it -- recreate from zero -- is the
    2026-09-02 incident. The message always names the artifacts a human
    needs to look at.
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


class _BoundCursorPath(type(Path())):
    """An absolute cursor path this module has already resolved.

    A marker type, not new behaviour: it lets :func:`_resolve_cursor_path`
    recognise its own output and hand it straight back, so a caller that
    binds once and passes the result to several calls really does get one
    resolution rather than one per call. Derived paths (``with_name`` for
    the lock, the claims, the state record) intentionally come back as
    this type too -- they are siblings of an already-bound path.
    """

    __slots__ = ()


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

    The returned path is always ABSOLUTE. Resolving once is not enough on
    its own: a relative ``cursor_path`` resolved once is still relative,
    and every later use of it -- the CAS re-read, the custody rename, the
    publish -- would be re-interpreted against the working directory at
    that moment. A ``chdir`` mid-mutation would then split the read from
    the write. Binding to an absolute path here is what makes "resolved
    once" mean "bound to one file".

    An absolute input resolves to itself regardless of cwd or
    environment, which is what lets a caller bind once per *tick* (see
    :func:`bind_phase1_cursor_path`) and hand the same bound path to
    every call it makes.
    """
    if isinstance(cursor_path, _BoundCursorPath):
        # Already bound by this module. Returning it untouched is what makes
        # "resolved once per tick" literally true rather than merely usually
        # true: a second ``resolve()`` re-walks the filesystem, so a symlink
        # or reparse point installed at the bound path between a tick's load
        # and its save would be followed on the way out and not on the way in.
        return cursor_path
    if cursor_path is not None:
        return _BoundCursorPath(Path(cursor_path).expanduser().resolve())
    return _BoundCursorPath(
        (resolve_manager_home(manager_home) / "runtime" / "phase1-cursor.json").resolve())


def bind_phase1_cursor_path(manager_home=None, cursor_path=None):
    """Resolve the cursor path once, for a caller that will make several calls.

    The Watcher tick loads, classifies, and later saves. Each of those
    used to resolve the path for itself, so a relative home plus a
    working-directory change between the load and the save made the tick
    read one file and advance a different one. A caller binds here at the
    START of its unit of work and passes the returned absolute path to
    every subsequent call; those calls re-derive the same path from it no
    matter what cwd or the environment do in between.
    """
    return _resolve_cursor_path(manager_home=manager_home, cursor_path=cursor_path)


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
    """Serialize read-compare-publish for one bound cursor path.

    A sidecar ``.lock`` file, never the cursor itself: locking the cursor
    would mean holding an open handle to the very file custody renames.
    The OS drops the lock when the holder exits, so a crashed writer
    cannot wedge the runtime -- which is why this is an OS lock and not a
    lock file whose staleness someone has to adjudicate.
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


def _fingerprint(path):
    """Identity of the exact bytes a mutation read, or None if absent.

    Content hash plus filesystem identity. The hash alone would miss a
    replace-with-identical-bytes; the identity alone would miss an
    in-place rewrite that kept the inode. Together they answer the only
    question custody cares about: "is this still the file I read?"
    """
    try:
        raw = path.read_bytes()
        st = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise CursorReadError(f"{path}: cannot fingerprint durable cursor: {exc}") from exc
    return (hashlib.sha256(raw).hexdigest(), st.st_dev, st.st_ino, st.st_size)


# --------------------------------------------------------------------------
# Durable artifacts beside the cursor
#
#   <cursor>                     the durable cursor; rotation authority
#   <cursor>.init-state          initialization-state record (validated)
#   <cursor>.claim-<txid>        a custody claim: the durable file, renamed
#                                out of the way by ONE mutation transaction
#   <cursor>.candidate-<txid>    a serialized candidate awaiting publication;
#                                never authoritative, safe to discard
#   <cursor>.lock                the mutation lock sidecar
#
# Claim and candidate names carry a fresh transaction id per attempt. A
# fixed claim name was how a second failed attempt overwrote the only
# surviving copy of the original cursor: the rename INTO the claim name
# clobbered what the previous failure had left there.
# --------------------------------------------------------------------------

INIT_STATE_SUFFIX = ".init-state"
#: Retained name from the previous round; the record it points at is now
#: a validated state record rather than an existence-only marker.
INIT_MARKER_SUFFIX = INIT_STATE_SUFFIX
#: The previous round's existence-only marker, under its own name. Renaming
#: the artifact must not amount to forgetting what it recorded: a deployment
#: that ran the previous round and then lost its cursor would otherwise show
#: "no record, no cursor" and be reinitialized from zero -- the exact reset
#: route this module exists to close. Read, never written.
LEGACY_INIT_MARKER_SUFFIX = ".initialized"
CLAIM_INFIX = ".claim-"
CANDIDATE_INFIX = ".candidate-"

INIT_STATE_SCHEMA = 2
INIT_PREPARED = "prepared"
INIT_COMMITTED = "committed"


def _new_txid():
    return uuid.uuid4().hex


def _init_state_path_for(path):
    return path.with_name(path.name + INIT_STATE_SUFFIX)


# Retained private name from the previous round for the existing tests.
_marker_path_for = _init_state_path_for


def _claim_path_for(path, txid):
    return path.with_name(f"{path.name}{CLAIM_INFIX}{txid}")


def _candidate_path_for(path, txid):
    return path.with_name(f"{path.name}{CANDIDATE_INFIX}{txid}")


def _siblings_with(path, infix):
    prefix = path.name + infix
    try:
        entries = list(path.parent.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise CursorReadError(f"{path.parent}: cannot list the cursor directory: {exc}") from exc
    return sorted(p for p in entries if p.name.startswith(prefix) and p.name != prefix)


def _list_claims(path):
    return _siblings_with(path, CLAIM_INFIX)


def _list_candidates(path):
    return _siblings_with(path, CANDIDATE_INFIX)


def _discard(name):
    """Remove a file that is NOT durable truth (a candidate, a temp). Best effort."""
    if name and os.path.exists(str(name)):
        try:
            os.unlink(str(name))
        except OSError:
            pass


def _write_exclusive_file(target, payload):
    """Create ``target`` (which must not exist) with ``payload`` as JSON, fsynced."""
    fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with open(fd, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        _discard(target)
        raise
    return str(target)


def _write_temp(path, payload):
    """Serialize a candidate beside the cursor under a unique, non-authoritative name."""
    return _write_exclusive_file(_candidate_path_for(path, _new_txid()), payload)


def _publish_exclusively(source, path):
    """Install ``source`` at ``path`` ONLY if ``path`` is absent. Never overwrites.

    This is the publication primitive, and it is the reason the last
    check and the install are no longer two statements with a gap
    between them: the operation itself refuses an occupied destination.

    * ``os.link`` is a no-overwrite operation on every platform this
      runs on (NTFS included: ``ERROR_ALREADY_EXISTS`` when the name is
      taken), and it is atomic.
    * Where hard links are unavailable, Windows' native rename is itself
      no-overwrite (``MoveFileEx`` without ``REPLACE_EXISTING``); on
      POSIX, where rename silently replaces, the fallback is an exclusive
      create, which still claims the NAME atomically.

    On success the source name is released (best effort). On an occupied
    destination :class:`FileExistsError` propagates and ``source`` is left
    exactly where it was, so a claim used as the source is never lost.
    """
    try:
        os.link(str(source), str(path))
    except FileExistsError:
        raise
    except (AttributeError, NotImplementedError, OSError) as exc:
        if isinstance(exc, OSError) and exc.errno == errno.EEXIST:
            raise FileExistsError(errno.EEXIST, str(exc), str(path)) from exc
        if os.name == "nt":
            os.rename(str(source), str(path))
            return
        # Claims the NAME atomically, but the bytes arrive afterwards, so a
        # reader can briefly see a partial file. Only reachable where hard
        # links are unavailable and the platform is not Windows -- never on
        # the production path. The identity check on the failure branch is
        # what stops the cleanup from deleting a competitor that replaced
        # this file while it was being written.
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            created = os.fstat(fd)
            with open(fd, "wb", closefd=True) as handle:
                handle.write(Path(source).read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                if os.path.samestat(os.stat(str(path)), created):
                    _discard(path)
            except (OSError, NameError):
                pass
            raise
    _discard(source)


# --------------------------------------------------------------------------
# Initialization state record
#
# "The cursor file is absent" and "the cursor has never existed" are not the
# same fact, and treating them as one is how a deleted generation-2459 cursor
# came back as generation 1 holding a single project. The cursor cannot
# answer the question -- its own absence is the condition being diagnosed --
# so the answer lives in a small VALIDATED record beside it.
#
# The record distinguishes an initialization that was PREPARED (intent
# recorded, cursor not yet created) from one that was COMMITTED (a cursor
# has existed here). An existence-only marker cannot: a crash between
# writing it and creating the cursor would look exactly like a lost cursor
# and wedge a clean first boot permanently.
#
# The record carries no generation, no project state and no rotation
# authority: it is provenance, not a second cursor. Nothing removes it
# automatically.
# --------------------------------------------------------------------------


def _legacy_marker_path_for(path):
    return path.with_name(path.name + LEGACY_INIT_MARKER_SUFFIX)


def _read_init_state(path):
    """None if no record exists, else INIT_PREPARED / INIT_COMMITTED. Anything else fails closed."""
    record_path = _init_state_path_for(path)
    try:
        raw = record_path.read_bytes()
    except FileNotFoundError:
        # No record under the current name. Before concluding "never
        # initialized", honour the previous round's existence-only marker
        # if one is present: it carries exactly one fact -- a cursor was
        # initialized here -- so it reads as COMMITTED. Its *contents*
        # are deliberately not parsed, because that marker never had a
        # trustworthy schema; only its presence is evidence.
        legacy = _legacy_marker_path_for(path)
        try:
            if legacy.exists():
                return INIT_COMMITTED
        except OSError as exc:
            raise CursorReadError(
                f"{legacy}: cannot stat the legacy initialization marker: {exc}") from exc
        return None
    except OSError as exc:
        raise CursorReadError(f"{record_path}: cannot read the initialization-state record: {exc}") from exc
    label = str(record_path)
    if not raw.strip():
        raise CursorInitStateError(f"{label}: initialization-state record is empty; refusing to guess")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CursorInitStateError(f"{label}: initialization-state record is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CursorInitStateError(f"{label}: initialization-state record must be an object")
    if data.get("schema") != INIT_STATE_SCHEMA:
        raise CursorInitStateError(
            f"{label}: initialization-state schema {data.get('schema')!r} is not {INIT_STATE_SCHEMA}")
    if data.get("cursor") != path.name:
        raise CursorInitStateError(
            f"{label}: initialization-state record is for cursor {data.get('cursor')!r}, not {path.name!r}")
    state = data.get("state")
    if state not in (INIT_PREPARED, INIT_COMMITTED):
        raise CursorInitStateError(f"{label}: initialization state {state!r} is not a known state")
    txid = data.get("txid")
    if not isinstance(txid, str) or not txid:
        raise CursorInitStateError(f"{label}: initialization-state record has no transaction id")
    return state


def _write_init_state(path, state, txid):
    """Durably record the initialization state. Atomic replace of the record only."""
    record_path = _init_state_path_for(path)
    payload = {
        "schema": INIT_STATE_SCHEMA,
        "cursor": path.name,
        "state": state,
        "txid": txid,
        "recorded_at": now_iso(),
    }
    temp_name = None
    try:
        temp_name = _write_exclusive_file(record_path.with_name(f"{record_path.name}.{txid}.tmp"), payload)
        # The record is provenance, not the cursor; replacing it in place
        # is fine and is what makes prepared -> committed a single step.
        os.replace(temp_name, str(record_path))
        temp_name = None
    except OSError as exc:
        raise CursorReadError(f"{record_path}: cannot record the initialization state: {exc}") from exc
    finally:
        _discard(temp_name)


def _commit_init_state(path, txid):
    """Record COMMITTED, idempotently. A cursor exists (or is about to be published)."""
    if _read_init_state(path) != INIT_COMMITTED:
        _write_init_state(path, INIT_COMMITTED, txid)


# --------------------------------------------------------------------------
# Recovery protocol
#
# Under the lock, before anything else, every claim beside the cursor is
# adjudicated. The rule set is small and every branch is deterministic:
#
#   cursor absent, any claim(s)      -> recovery required (the claim IS the
#                                       last durable copy; never touched)
#   cursor present, claim strictly   -> the claim is a superseded copy from
#     older (or a duplicate of the      a transaction that published but
#     same copy)                        could not tidy up; retire it
#   cursor present, claim not older  -> recovery required (a human decides
#     or either side unreadable         which copy is authoritative)
#
# Then the initialization record:
#
#   record invalid                   -> fail closed (CursorInitStateError)
#   cursor present, none/prepared    -> adopt: commit BEFORE any custody
#   cursor present, committed        -> live
#   cursor absent, none              -> genuine first boot
#   cursor absent, prepared          -> resume first boot (intent recorded,
#                                       cursor never created)
#   cursor absent, committed         -> recovery required
# --------------------------------------------------------------------------


def _claim_is_redundant(held, held_fingerprint, current, current_fingerprint):
    """Is every fact in ``held`` already carried by ``current``? (redundant, why-not).

    A lower generation is NOT sufficient, and assuming it was is how an
    independent review destroyed a 13-project generation-2458 claim
    against a 1-project generation-2500 cursor that an external writer
    had installed: the newer file was newer and poorer, and deleting the
    claim erased the only copy of the missing twelve projects.

    Retirement therefore requires proof of redundancy: identical bytes,
    or a strictly older generation whose per-project coverage the
    current cursor already contains at a value at least as advanced.
    Anything else is a human's decision.
    """
    if held_fingerprint is not None and current_fingerprint is not None \
            and held_fingerprint[0] == current_fingerprint[0]:
        return True, ""
    if held["generation"] >= current["generation"]:
        return False, "the claim's generation is not older"
    for field in ("per_project_record_cursor", "per_project_attention_visits"):
        held_map, current_map = held[field], current[field]
        missing = sorted(set(held_map) - set(current_map))
        if missing:
            return False, (f"the claim carries {field} entries the durable cursor has lost "
                           f"({', '.join(missing[:5])}{'...' if len(missing) > 5 else ''})")
        regressed = sorted(k for k, v in held_map.items() if current_map[k] < v)
        if regressed:
            return False, (f"the durable cursor's {field} went backwards for "
                           f"{', '.join(regressed[:5])}{'...' if len(regressed) > 5 else ''}")
    return True, ""


def _adjudicate_claims(path, claims):
    """Retire provably redundant claims; fail closed on anything else. Never deletes a last copy."""
    if not claims:
        return
    names = ", ".join(c.name for c in claims)
    if not _exists_at(path):
        if len(claims) == 1:
            raise CursorRecoveryRequiredError(
                f"{path}: no durable cursor exists, but custody claim {claims[0].name} holds the last "
                "durable copy from an interrupted mutation. Refusing to mutate or reinitialize. "
                f"To recover, rename {claims[0]} back to {path} by hand (only while {path.name} is "
                "absent); nothing here will delete it."
            )
        raise CursorRecoveryRequiredError(
            f"{path}: no durable cursor exists and {len(claims)} custody claims are present ({names}). "
            "Refusing to choose between them automatically; a human must determine the authoritative "
            "copy. Nothing here will delete any of them."
        )
    try:
        current = _load_at(path, missing_ok=False)
        current_fingerprint = _fingerprint(path)
    except PhaseCursorError as exc:
        raise CursorRecoveryRequiredError(
            f"{path}: custody claims are present ({names}) and the durable cursor cannot be read to "
            f"adjudicate them: {exc}. Refusing to mutate."
        ) from exc
    if current_fingerprint is None:
        raise CursorRecoveryRequiredError(
            f"{path}: custody claims are present ({names}) and the durable cursor vanished while being "
            "read; refusing to mutate."
        )
    for claim in claims:
        try:
            held = _load_at(claim, missing_ok=False)
            held_fingerprint = _fingerprint(claim)
        except CursorMissingError:
            continue  # raced away by the transaction that owned it
        except CursorStateError as exc:
            raise CursorRecoveryRequiredError(
                f"{path}: custody claim {claim.name} cannot be read ({exc}); refusing to mutate until a "
                "human has adjudicated it. Nothing here will delete it."
            ) from exc
        redundant, why = _claim_is_redundant(held, held_fingerprint, current, current_fingerprint)
        if not redundant:
            raise CursorRecoveryRequiredError(
                f"{path}: custody claim {claim.name} (generation {held['generation']}, "
                f"{len(held['per_project_record_cursor'])} projects) is not a superseded copy of the "
                f"durable cursor (generation {current['generation']}, "
                f"{len(current['per_project_record_cursor'])} projects): {why}. A human must decide "
                "which is authoritative. Nothing here will delete either."
            )
        try:
            os.unlink(str(claim))
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise CursorReadError(f"{claim}: cannot retire the superseded custody claim: {exc}") from exc


def _sweep_candidates(path):
    """Candidates are never truth. Under the lock, none can be in flight but ours."""
    for candidate in _list_candidates(path):
        _discard(candidate)


def _swap_under_custody(path, expected_generation, build_payload, fingerprint):
    """Publish a successor to the durable file, deciding everything under custody.

    The custody transaction, in order:

    1. the durable file is renamed to a UNIQUE claim name -- an atomic
       operation that gives this transaction sole custody of one file;
    2. the claim is verified to be the exact bytes the pre-custody
       compare looked at; if it is not, or it is gone, nothing happens;
    3. the claim is RE-PARSED, and that parse -- not the caller's
       earlier read -- is the authoritative durable state. Its
       generation must still equal ``expected_generation``;
    4. the successor is built from that authoritative state and
       serialized under a unique non-authoritative candidate name;
    5. the candidate is published with a NO-OVERWRITE primitive. If
       anything -- anyone -- has installed a file at the canonical name
       since custody vacated it, publication fails by construction, that
       file survives, and the claim is preserved for adjudication;
    6. only after a successful publish is the claim redundant.

    Step 3 is not redundant with step 2. An independent review installed
    generation 2500 in the window between the pre-custody *parse* and
    the pre-custody *fingerprint*: the fingerprint then described the
    2500 file, custody verified it against itself, and a payload
    computed from the stale 2458 parse was published as 2459. Both the
    generation check and the merge base therefore have to come from a
    file this transaction already owns.

    There is no unconditional replace over the canonical name anywhere
    in this sequence. A failure at any step leaves the last durable copy
    on disk under either its own name or the claim name; the claim is
    never deleted on a failure path.
    """
    txid = _new_txid()
    claim = _claim_path_for(path, txid)
    temp_name = None
    claimed = False
    try:
        try:
            os.replace(str(path), str(claim))
            claimed = True
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise StaleCursorError(
                f"{path}: durable cursor disappeared before custody; refusing to recreate it"
            ) from exc
        except OSError as exc:
            raise CursorReadError(f"{path}: cannot take custody of durable cursor: {exc}") from exc

        held = _fingerprint(claim)
        if held is None:
            raise StaleCursorError(
                f"{path}: the durable cursor vanished from custody ({claim.name}); refusing to publish "
                "over a state that no longer exists"
            )
        if held != fingerprint:
            raise StaleCursorError(
                f"{path}: durable cursor changed between the compare and custody; "
                "refusing to overwrite a newer writer's state"
            )

        # Authoritative read: this file is private to this transaction now.
        durable = _load_at(claim, missing_ok=False)
        if durable["generation"] != expected_generation:
            raise StaleCursorError(
                f"Cursor generation mismatch under custody: expected {expected_generation}, "
                f"found {durable['generation']}; refusing to publish a successor to state this "
                "mutation never read"
            )
        payload = build_payload(durable)
        temp_name = _write_temp(path, payload)

        try:
            _publish_exclusively(temp_name, path)
        except FileExistsError as exc:
            raise StaleCursorError(
                f"{path}: another writer installed a durable cursor while this mutation held custody; "
                f"that cursor is preserved and this write is refused. Custody claim {claim.name} is "
                "kept for adjudication."
            ) from exc
        temp_name = None
        # Only now is the claim redundant: the new file is in place, so
        # the copy it holds is genuinely superseded. If this unlink fails
        # the claim stays; the next mutation retires it as superseded.
        try:
            os.unlink(str(claim))
        except OSError:
            pass
        return payload
    except BaseException:
        _discard(temp_name)
        if claimed and os.path.exists(str(claim)):
            # Put the durable file back under its own name -- but only if
            # that name is still vacant. If someone else now occupies it,
            # the claim stays where it is: it may be the older copy or it
            # may be the authoritative one, and the recovery protocol
            # decides that on the next mutation, deterministically, with
            # both copies still on disk.
            try:
                _publish_exclusively(claim, path)
            except (FileExistsError, OSError):
                # The claim is now the ONLY copy of the durable state (or
                # one of two copies that need adjudicating). It is
                # deliberately LEFT ON DISK. Any existing claim means the
                # next mutation fails closed rather than starting over.
                pass
        raise


def _create_exclusively(path, payload):
    """Create a cursor that does not exist. Atomic against a racing creator."""
    temp_name = _write_temp(path, payload)
    try:
        _publish_exclusively(temp_name, path)
    except FileExistsError as exc:
        _discard(temp_name)
        raise StaleCursorError(
            f"{path}: CREATE_ONLY write refused, a durable cursor appeared during the write"
        ) from exc
    except BaseException:
        _discard(temp_name)
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


def phase1_cursor_init_state(manager_home=None, cursor_path=None):
    """None (never initialized), 'prepared' or 'committed'. Invalid records fail closed."""
    return _read_init_state(_resolve_cursor_path(manager_home=manager_home, cursor_path=cursor_path))


def require_phase1_cursor_first_boot(manager_home=None, cursor_path=None):
    """Prove that creating a generation-1 cursor here would be a genuine first boot.

    Returns normally only when the durable artifacts say so: no cursor,
    no custody claim, and an initialization record that is absent or
    merely PREPARED. Every other combination raises:

    * :class:`StaleCursorError` -- a cursor is present (amend it instead);
    * :class:`CursorRecoveryRequiredError` -- a claim exists, or the
      record says a cursor was COMMITTED here before;
    * :class:`CursorInitStateError` -- the record is unreadable garbage.

    Advisory in the same sense as :func:`phase1_cursor_exists`: the save
    re-proves it under the lock.
    """
    path = _resolve_cursor_path(manager_home=manager_home, cursor_path=cursor_path)
    _adjudicate_first_boot(path)
    return True


def _adjudicate_first_boot(path):
    """Under-lock (or advisory) proof that a CREATE_ONLY write is a genuine first boot."""
    claims = _list_claims(path)
    if claims:
        # With the cursor absent this is recovery-required; with it present
        # it is either superseded debris or a conflict -- but a CREATE_ONLY
        # caller with a present cursor is stale either way.
        _adjudicate_claims(path, claims)
    if _exists_at(path):
        raise StaleCursorError(f"{path}: CREATE_ONLY write refused, a durable cursor already exists")
    state = _read_init_state(path)
    if state == INIT_COMMITTED:
        raise CursorRecoveryRequiredError(
            f"{path}: no durable cursor exists, but {_init_state_path_for(path).name} records that one "
            "was committed here before. Refusing to silently reinitialize Phase-1 rotation from zero. "
            "Investigate why the cursor vanished; to deliberately start over, remove "
            f"{_init_state_path_for(path)} by hand."
        )
    return state


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

    * :data:`CREATE_ONLY` -- create a cursor that does not exist yet, and
      only when the durable artifacts prove this is a genuine first boot.
      The created cursor is generation 1.
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
    custody rename and the publish all use that one bound path, inside
    the per-cursor mutation lock. The full custody transaction and its
    recovery semantics are described on :func:`_swap_under_custody` and
    in the "Recovery protocol" comment above.
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
        # Recovery first: an unresolved claim from an interrupted
        # transaction blocks ordinary mutation until it is adjudicated,
        # and candidates from interrupted transactions are just debris.
        _sweep_candidates(path)
        txid = _new_txid()

        if creating:
            state = _adjudicate_first_boot(path)
            durable = None
            base_generation = 0
            fingerprint = None
        else:
            _adjudicate_claims(path, _list_claims(path))
            state = _read_init_state(path)
            # The compare half of the compare-and-swap. `missing_ok=False`,
            # so an absent file raises instead of presenting generation 0
            # -- "there is nothing here" and "here is a generation-0
            # cursor" must not be the same answer.
            try:
                durable = _load_at(path, missing_ok=False)
            except CursorMissingError as exc:
                if state == INIT_COMMITTED:
                    raise CursorRecoveryRequiredError(
                        f"{path}: write expected generation {expected_generation} but no durable cursor "
                        f"exists, and {_init_state_path_for(path).name} records that one was committed "
                        "here before. Refusing to mutate; investigate why the cursor vanished."
                    ) from exc
                raise StaleCursorError(
                    f"{path}: write expected generation {expected_generation} but no durable cursor exists; "
                    "pass CREATE_ONLY to create one"
                ) from exc
            base_generation = durable["generation"]
            if base_generation != expected_generation:
                raise StaleCursorError(
                    f"Cursor generation mismatch: expected {expected_generation}, found {base_generation}"
                )
            # Bind the exact bytes this decision was made on, for custody.
            fingerprint = _fingerprint(path)
            if fingerprint is None:
                raise StaleCursorError(
                    f"{path}: durable cursor disappeared during the compare; refusing to recreate it"
                )

        def build_payload(durable_state):
            """The successor to ``durable_state``. Called with authoritative state only.

            For an amendment that means the re-parse taken under custody,
            never the caller's snapshot and never the pre-custody read --
            both the next generation and the merge base have to come
            from a file this mutation owns.
            """
            # Authoritative: durable state + 1, never the caller's snapshot.
            project_cursor = int(cursor_data.get("project_cursor", 0))
            if project_cursor < 0:
                project_cursor = 0

            # Merge, never replace: per-project state the durable cursor holds and
            # this snapshot does not mention is carried forward. Nothing in the
            # runtime deletes a project's cursor entry, so there is no deletion
            # contract to preserve here; adding one would need its own explicit
            # verb rather than an omission from a partial snapshot.
            record_cursors = dict(durable_state["per_project_record_cursor"]) if durable_state else {}
            record_cursors.update(_clean_counter_map(cursor_data.get("per_project_record_cursor", {})))
            attention_visits = dict(durable_state["per_project_attention_visits"]) if durable_state else {}
            attention_visits.update(_clean_counter_map(cursor_data.get("per_project_attention_visits", {})))
            return {
                "project_cursor": project_cursor,
                "per_project_record_cursor": record_cursors,
                "per_project_attention_visits": attention_visits,
                "generation": (durable_state["generation"] if durable_state else 0) + 1,
                "updated_at": now_iso(),
            }

        if creating:
            payload = build_payload(None)
            # First-boot transaction: PREPARE (intent, durable) -> create
            # the cursor -> COMMIT. A crash after PREPARE leaves "prepared
            # + no cursor", which the next attempt resumes; a crash after
            # the create leaves a valid cursor that the next mutation
            # adopts and commits. Neither wedges, and neither can be
            # mistaken for a lost committed cursor.
            if state != INIT_PREPARED:
                _write_init_state(path, INIT_PREPARED, txid)
            _create_exclusively(path, payload)
            try:
                _commit_init_state(path, txid)
            except BaseException:
                # The cursor exists but nothing durable says so yet. If it
                # were left, a later loss would show PREPARED + absent --
                # indistinguishable from "never created" -- and authorise
                # a second generation-1 creation. Roll the creation back
                # instead: this cursor is generation 1 and carries nothing
                # a later first boot will not recreate, so discarding it
                # loses no rotation state, while leaving it opens a route
                # to a false first boot.
                _discard(path)
                raise
        else:
            # Upgrade / adopt fence, BEFORE custody: a valid cursor that
            # predates the record (the one live in production), or one
            # created by a first boot that crashed before its commit, is
            # committed now. From this statement on there is no instant at
            # which the historical cursor has left its name while no
            # durable evidence says it ever existed.
            if state != INIT_COMMITTED:
                _commit_init_state(path, txid)
            payload = _swap_under_custody(path, expected_generation, build_payload, fingerprint)

    return payload
