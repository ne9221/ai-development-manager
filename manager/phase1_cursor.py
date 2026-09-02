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

import errno
import hashlib
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


class CursorRecoveryRequiredError(PhaseCursorError):
    """A durable cursor that once existed is gone, and a human must say why.

    Deliberately NOT a :class:`CursorStateError` and NOT a
    :class:`CursorMissingError`: it is neither ordinary corruption nor a
    legitimate first boot, and nothing may catch it as either. It is the
    one condition the runtime cannot resolve on its own, because every
    automatic resolution available to it -- recreate from zero -- is the
    2026-09-02 incident.
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

    The returned path is always ABSOLUTE. Resolving once is not enough on
    its own: a relative ``cursor_path`` resolved once is still relative,
    and every later use of it -- the CAS re-read, the pre-replace check,
    ``os.replace`` -- would be re-interpreted against the working
    directory at that moment. A ``chdir`` mid-mutation would then split
    the read from the write. Binding to an absolute path here is what
    makes "resolved once" mean "bound to one file".
    """
    if cursor_path is not None:
        return Path(cursor_path).expanduser().resolve()
    return (resolve_manager_home(manager_home) / "runtime" / "phase1-cursor.json").resolve()


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


def _fingerprint(path):
    """Identity of the exact bytes a mutation read, or None if absent.

    Content hash plus filesystem identity. The hash alone would miss a
    replace-with-identical-bytes; the identity alone would miss an
    in-place rewrite that kept the inode. Together they answer the only
    question the swap cares about: "is this still the file I read?"
    """
    try:
        raw = path.read_bytes()
        st = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise CursorReadError(f"{path}: cannot fingerprint durable cursor: {exc}") from exc
    return (hashlib.sha256(raw).hexdigest(), st.st_dev, st.st_ino, st.st_size)


def _write_temp(path, payload):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        return handle.name


def _discard(temp_name):
    if temp_name and os.path.exists(temp_name):
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def _swap_under_custody(path, payload, fingerprint):
    """Replace `path`, but only if it is still the file `fingerprint` describes.

    ``os.replace`` is atomic but UNCONDITIONAL: it will happily overwrite
    a file that changed since the caller last looked. Checking first and
    replacing second leaves a window in between, and an independent
    review drove a generation-2500 write into exactly that window and
    watched it get clobbered by a generation-2459 one.

    So the durable file is first RENAMED out of the way, into a private
    claim name. That rename is atomic and gives this writer sole custody
    of one specific file. Verification then happens on the claim -- a
    file nobody else can reach by name any more -- rather than on a path
    a competitor is still free to write. If the claim is not what was
    read, or something has since recreated the original path, the claim
    is put back and the write is refused.

    What this does NOT claim: the instant between the last check and
    ``os.replace`` is still not zero, and no user-space code can make it
    zero without an OS-level conditional rename, which Windows does not
    offer. What it does buy is that a competing writer has to land inside
    a window bounded by two adjacent statements, instead of anywhere in a
    read-verify-write sequence that also does JSON encoding and file I/O.
    Cooperating writers -- every writer in this repo -- take the mutation
    lock and cannot be in that window at all.
    """
    claim = path.with_name(f"{path.name}.claim-{os.getpid()}-{threading.get_ident():x}")
    # Serialise the payload BEFORE taking custody. Everything slow -- JSON
    # encoding, file creation, fsync -- has to happen outside the checked
    # window, or it becomes part of it. What remains between the last
    # verification and the swap is one statement.
    temp_name = _write_temp(path, payload)
    claimed = False
    try:
        try:
            os.replace(str(path), str(claim))
            claimed = True
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise StaleCursorError(
                f"{path}: durable cursor disappeared before the swap; refusing to recreate it"
            ) from exc
        except OSError as exc:
            raise CursorReadError(f"{path}: cannot take custody of durable cursor: {exc}") from exc

        # Now that the file is ours alone, confirm it is the one we read.
        if _fingerprint(claim) != fingerprint:
            raise StaleCursorError(
                f"{path}: durable cursor changed between the compare and the swap; "
                "refusing to overwrite a newer writer's state"
            )
        # And confirm nobody recreated the original name while it was vacant.
        if os.path.exists(str(path)):
            raise StaleCursorError(
                f"{path}: a durable cursor reappeared during the swap; refusing to overwrite it"
            )

        os.replace(temp_name, str(path))
        temp_name = None
        # Only now is the claim redundant: the new file is in place, so
        # the copy it holds is genuinely superseded.
        _discard(str(claim))
    except Exception:
        _discard(temp_name)
        if claimed and os.path.exists(str(claim)):
            # Put the file back exactly as found. Aborting must leave the
            # durable state untouched, not merely unwritten.
            if os.path.exists(str(path)):
                # Something newer already occupies the durable name, so
                # the claim is a superseded copy and may go.
                _discard(str(claim))
            else:
                try:
                    os.replace(str(claim), str(path))
                except OSError:
                    # The restore failed too. The claim is now the ONLY
                    # copy of the durable state, so it is deliberately
                    # LEFT ON DISK. Deleting it here to keep the
                    # directory tidy would destroy the very cursor this
                    # module exists to protect -- and the initialization
                    # marker guarantees the next tick fails closed rather
                    # than starting over, so a human finds the claim
                    # rather than a silently reset rotation.
                    pass
        raise


def _create_exclusively(path, payload):
    """Create a cursor that does not exist. Atomic against a racing creator."""
    temp_name = _write_temp(path, payload)
    try:
        # O_CREAT|O_EXCL on a link target is the atomic "create if absent"
        # primitive; os.replace would silently clobber a competitor.
        try:
            os.link(temp_name, str(path))
        except (AttributeError, NotImplementedError, OSError) as exc:
            if isinstance(exc, OSError) and exc.errno == errno.EEXIST:
                raise StaleCursorError(
                    f"{path}: CREATE_ONLY write refused, a durable cursor appeared during the write"
                ) from exc
            # Filesystems without hard links: fall back to an exclusive
            # create, which is still atomic against another creator.
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with open(fd, "w", encoding="utf-8", closefd=True) as handle:
                    handle.write(Path(temp_name).read_text(encoding="utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                try:
                    os.unlink(str(path))
                except OSError:
                    pass
                raise
    except FileExistsError as exc:
        raise StaleCursorError(
            f"{path}: CREATE_ONLY write refused, a durable cursor appeared during the write"
        ) from exc
    finally:
        _discard(temp_name)


# --------------------------------------------------------------------------
# Initialization provenance
#
# "The cursor file is absent" and "the cursor has never existed" are not the
# same fact, and treating them as one is how a deleted generation-2459 cursor
# came back as generation 1 holding a single project. The cursor file cannot
# answer the question -- its own absence is the condition being diagnosed --
# so the answer lives in a separate durable marker beside it.
#
# The marker's ONLY truth is "a Phase-1 cursor has been initialized here
# before". It deliberately carries no generation, no project state and no
# rotation authority: it is a fence, not a second cursor. Nothing removes it
# automatically; clearing it is a human recovery decision, which is the point.
# --------------------------------------------------------------------------

#: Sibling of the cursor it fences, named after it. Deliberately derived
#: from the cursor's FILE name rather than being a fixed name in its
#: directory: two cursors can share a directory, and a marker keyed only
#: on the directory would let one cursor's history fence another's.
INIT_MARKER_SUFFIX = ".initialized"


def _marker_path_for(path):
    return path.with_name(path.name + INIT_MARKER_SUFFIX)


def _marker_exists(marker):
    try:
        return marker.exists()
    except OSError as exc:
        raise CursorReadError(f"{marker}: cannot stat the initialization marker: {exc}") from exc


def _establish_marker(marker):
    """Idempotently record that a cursor has existed here. Never removes."""
    if _marker_exists(marker):
        return
    payload = {"schema": 1, "initialized_at": now_iso()}
    temp_name = None
    try:
        temp_name = _write_temp(marker, payload)
        try:
            os.link(temp_name, str(marker))
        except (AttributeError, NotImplementedError, OSError):
            # A loser of the creation race is fine: the marker is a fact,
            # not a claim, so any winner records the same fact.
            if not _marker_exists(marker):
                os.replace(temp_name, str(marker))
                temp_name = None
    except OSError as exc:
        raise CursorReadError(f"{marker}: cannot record the initialization marker: {exc}") from exc
    finally:
        _discard(temp_name)


def phase1_cursor_initialized(manager_home=None, cursor_path=None):
    """Whether a Phase-1 cursor has ever been initialized under this home."""
    return _marker_exists(_marker_path_for(
        _resolve_cursor_path(manager_home=manager_home, cursor_path=cursor_path)))


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
        marker = _marker_path_for(path)
        if creating:
            if _exists_at(path):
                raise StaleCursorError(
                    f"{path}: CREATE_ONLY write refused, a durable cursor already exists"
                )
            # The fence. Absent cursor + established marker means the
            # cursor existed and is now gone -- deletion, a failed swap, a
            # wiped runtime directory. Recreating it here is what turned a
            # generation-2459 cursor covering 13 projects into a
            # generation-1 one covering 1. A human has to decide what
            # happened; this refuses rather than inventing a new history.
            if _marker_exists(marker):
                raise CursorRecoveryRequiredError(
                    f"{path}: no durable cursor exists, but {marker.name} records that one was "
                    "initialized here before. Refusing to silently reinitialize Phase-1 rotation "
                    "from zero. Investigate why the cursor vanished; to deliberately start over, "
                    f"remove {marker} by hand."
                )
            durable = None
            base_generation = 0
            fingerprint = None
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
            # Bind the exact bytes this decision was made on, for the swap.
            fingerprint = _fingerprint(path)
            if fingerprint is None:
                raise StaleCursorError(
                    f"{path}: durable cursor disappeared during the compare; refusing to recreate it"
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

        # The swap. Both branches carry their verification INTO the write
        # rather than performing it beforehand and hoping: an earlier
        # version checked here and replaced afterwards, and a competing
        # generation-2500 write landed in between and was clobbered.
        if creating:
            # Marker first. If this process dies between the two writes,
            # the next boot sees "initialized before, cursor absent" and
            # fails closed -- the safe direction. The reverse order would
            # leave a cursor no marker vouches for, and its later loss
            # would once again look like a first boot.
            _establish_marker(marker)
            _create_exclusively(path, payload)
        else:
            _swap_under_custody(path, payload, fingerprint)
            # An existing cursor proves initialization just as well as
            # creating one does. Stamping it here is what closes the gap
            # for the cursor already live in production, which predates
            # the marker: its first successful tick after activation
            # fences it, so a later disappearance fails closed.
            _establish_marker(marker)

    return payload
