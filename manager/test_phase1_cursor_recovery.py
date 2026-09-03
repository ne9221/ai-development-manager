"""Recovery-protocol regression suite for the Phase-1 cursor custody transaction.

Round 3. An independent review of the previous round drove four
reproductions, each of which is re-run here through the same code paths
the reviewer used (the primitive, and the real ``poll_once`` tick):

* **Upgrade double failure.** A valid generation-2458 cursor with no
  initialization record; publication fails AND the restore fails. The
  next tick saw "no cursor, no marker" and started over at generation 1,
  and a later attempt reused the fixed claim name and overwrote the only
  surviving copy of the original.
* **First-boot interruption.** A genuine first boot that recorded its
  marker and died before creating the cursor wedged permanently: every
  later tick saw "marker present, cursor absent" and refused.
* **External insert after custody.** Generation 2500 installed at the
  canonical name after the last check but before ``os.replace`` was
  clobbered by 2459, because ``os.replace`` overwrites unconditionally.
* **Whole-Watcher path hijack.** The tick resolved the cursor path on
  load and again on save, so a relative home plus a cwd change in
  between read the original and advanced a decoy.

The numbers 2458 / 13 / 2500 are the reviewer's.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import manager.phase1_cursor as pc
from manager.phase1_cursor import (
    CLAIM_INFIX,
    CREATE_ONLY,
    INIT_COMMITTED,
    INIT_PREPARED,
    INIT_STATE_SUFFIX,
    CursorInitStateError,
    CursorRecoveryRequiredError,
    CursorStateError,
    StaleCursorError,
    bind_phase1_cursor_path,
    load_phase1_cursor,
    phase1_cursor_init_state,
    require_phase1_cursor_first_boot,
    save_phase1_cursor,
)
from manager.test_phase1_cursor_integrity import fail_canonical_install

PROD_13_PROJECTS = {f"proj-{i:02d}": 100 + i for i in range(13)}


def _tasks(pid):
    from manager.trusted_ingress import TRUSTED_INGRESS_ORIGIN
    return [{"project_id": pid, "task_id": "%s-t%d" % (pid, i), "title": "T",
             "status": "queued", "recommended_provider": None,
             "quota_evidence": {"codex": {}},
             "source_context": {"origin": TRUSTED_INGRESS_ORIGIN}} for i in range(6)]


def watcher_tick(cursor_path, midway=None):
    """One real ``poll_once`` tick over two in-memory projects.

    ``midway`` runs once, after the tick has loaded the cursor and before
    it saves -- the window the path-hijack reproduction targets.
    """
    from manager.command_watcher import poll_once
    import manager.command_watcher as cw
    from manager.test_phase1_fair_scheduling import MemoryDiscoveryStore

    store = MemoryDiscoveryStore({p: _tasks(p) for p in ("p0", "p1")})
    real_enumerate = cw._enumerate_recent_commands
    fired = []

    def enumerate_hook(*args, **kwargs):
        if midway is not None and not fired:
            fired.append(1)
            midway()
        return real_enumerate(*args, **kwargs)

    with patch("manager.command_watcher.read_drive_status", return_value={"codex": {"status": "available"}}), \
            patch.object(cw, "_enumerate_recent_commands", side_effect=enumerate_hook):
        return poll_once(store, None, discovery_store=store, cursor_path=cursor_path, allowlist=frozenset())


class RecoveryTestCase(unittest.TestCase):
    """A throwaway manager home per test. Never the production one."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="adm-cursor-recovery-"))
        self.runtime = self.home / "runtime"
        self.runtime.mkdir(parents=True)
        self.cursor_path = self.runtime / "phase1-cursor.json"
        self.state_path = self.runtime / ("phase1-cursor.json" + INIT_STATE_SUFFIX)
        self.addCleanup(shutil.rmtree, self.home, True)

    def seed(self, generation=2458, records=None, path=None):
        path = path or self.cursor_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "project_cursor": 0,
            "per_project_record_cursor": dict(PROD_13_PROJECTS) if records is None else records,
            "per_project_attention_visits": {},
            "generation": generation,
            "updated_at": "2026-09-02T23:31:20Z",
        }), encoding="utf-8")

    def durable(self, path=None):
        return json.loads((path or self.cursor_path).read_text(encoding="utf-8"))

    def save(self, cursor_data, **kwargs):
        return save_phase1_cursor(cursor_data, cursor_path=self.cursor_path, **kwargs)

    def claims(self):
        return sorted(p for p in self.runtime.iterdir() if CLAIM_INFIX in p.name)

    def state(self):
        return phase1_cursor_init_state(cursor_path=self.cursor_path)

    def write_state(self, state, txid="t-1", **extra):
        body = {"schema": 2, "cursor": self.cursor_path.name, "state": state, "txid": txid,
                "recorded_at": "2026-09-03T00:00:00Z"}
        body.update(extra)
        self.state_path.write_text(json.dumps(body), encoding="utf-8")

    def fail_double(self):
        """Publication AND restore fail: the reviewer's 'swap fails, restore fails'."""
        return fail_canonical_install(self.cursor_path)


# ---------------------------------------------------------------------------
# Blocker 1 -- upgrade double failure
# ---------------------------------------------------------------------------


class TestUpgradeDoubleFailure(RecoveryTestCase):

    def test_reviewer_reproduction_through_two_watcher_ticks(self):
        """2458/13, no record; publish and restore fail; two ticks; nothing resets, nothing is lost."""
        self.seed(2458)
        self.assertIsNone(self.state())
        with self.fail_double():
            with self.assertRaises(OSError):
                self.save({"project_cursor": 1}, expected_generation=2458)
        claims = self.claims()
        self.assertEqual(1, len(claims))
        self.assertEqual(2458, self.durable(claims[0])["generation"])
        self.assertFalse(self.cursor_path.exists())
        # The fence was established BEFORE custody, so the next tick knows.
        self.assertEqual(INIT_COMMITTED, self.state())

        watcher_tick(str(self.cursor_path))
        watcher_tick(str(self.cursor_path))

        self.assertFalse(self.cursor_path.exists(), "a tick reinitialized a cursor whose last copy is in a claim")
        self.assertEqual(claims, self.claims(), "a tick touched the custody claim")
        self.assertEqual(2458, self.durable(claims[0])["generation"])
        self.assertEqual(13, len(self.durable(claims[0])["per_project_record_cursor"]))

    def test_leftover_claim_prevents_first_init_even_without_a_record(self):
        """ANY existing claim means NOT first boot -- with no record at all."""
        self.seed(2458, path=self.runtime / ("phase1-cursor.json" + CLAIM_INFIX + "deadbeef"))
        self.assertIsNone(self.state())
        with self.assertRaises(CursorRecoveryRequiredError):
            require_phase1_cursor_first_boot(cursor_path=self.cursor_path)
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        self.assertFalse(self.cursor_path.exists())
        self.assertEqual(1, len(self.claims()))
        watcher_tick(str(self.cursor_path))
        self.assertFalse(self.cursor_path.exists())
        self.assertEqual(1, len(self.claims()))

    def test_claim_names_are_unique_and_never_reused(self):
        """Two interrupted transactions in one process leave two distinct claims."""
        self.seed(2458)
        with self.fail_double():
            with self.assertRaises(OSError):
                self.save({"project_cursor": 1}, expected_generation=2458)
        first = self.claims()
        self.assertEqual(1, len(first))
        # A human puts the cursor back (the documented recovery)...
        os.rename(str(first[0]), str(self.cursor_path))
        self.assertEqual(2459, self.save({"project_cursor": 1}, expected_generation=2458)["generation"])
        # ...and the next transaction fails the same way. Every rename must
        # land on a name that did not exist the instant before.
        real_replace = os.replace
        destinations = []

        def never_over_existing(src, dst):
            destinations.append(str(dst))
            self.assertFalse(os.path.lexists(str(dst)), f"rename over an existing file: {dst}")
            return real_replace(src, dst)

        with patch.object(pc.os, "replace", side_effect=never_over_existing), self.fail_double():
            with self.assertRaises(OSError):
                self.save({"project_cursor": 2}, expected_generation=2459)
        second = self.claims()
        self.assertEqual(1, len(second))
        self.assertNotEqual(first[0].name, second[0].name)
        self.assertEqual(2459, self.durable(second[0])["generation"])
        self.assertTrue(any(CLAIM_INFIX in d for d in destinations))
        # And two ids from the generator never collide.
        self.assertNotEqual(pc._claim_path_for(self.cursor_path, pc._new_txid()),
                            pc._claim_path_for(self.cursor_path, pc._new_txid()))

    def test_fence_is_durable_before_custody(self):
        """No instant exists where the historical cursor has left its name with no record."""
        self.seed(2458)
        real_replace = os.replace
        seen = {}

        def custody(src, dst):
            if CLAIM_INFIX in str(dst):
                seen["state_at_custody"] = self.state()
                raise OSError("injected: custody refused")
            return real_replace(src, dst)

        with patch.object(pc.os, "replace", side_effect=custody):
            with self.assertRaises(CursorStateError):
                self.save({"project_cursor": 1}, expected_generation=2458)
        self.assertEqual(INIT_COMMITTED, seen["state_at_custody"])
        self.assertEqual(2458, self.durable()["generation"])
        self.assertEqual([], self.claims())

    def test_upgrade_preserves_generation_and_coverage(self):
        self.seed(2458)
        saved = self.save({"project_cursor": 3, "per_project_record_cursor": {"proj-00": 999}},
                          expected_generation=2458)
        self.assertEqual(2459, saved["generation"])
        self.assertEqual(13, len(saved["per_project_record_cursor"]))
        self.assertEqual(999, saved["per_project_record_cursor"]["proj-00"])
        self.assertEqual(INIT_COMMITTED, self.state())


# ---------------------------------------------------------------------------
# Blocker 2 -- first-boot transaction
# ---------------------------------------------------------------------------


class TestFirstBootTransaction(RecoveryTestCase):

    def test_crash_before_prepare_retries_first_boot(self):
        self.assertIsNone(self.state())
        self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        self.assertEqual(1, self.durable()["generation"])
        self.assertEqual(INIT_COMMITTED, self.state())

    def test_crash_after_prepare_before_create_resumes(self):
        """The reviewer's wedge: prepared record, no cursor. The next tick must recover."""
        with fail_canonical_install(self.cursor_path):
            with self.assertRaises(OSError):
                self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        self.assertFalse(self.cursor_path.exists())
        self.assertEqual(INIT_PREPARED, self.state())
        self.assertTrue(require_phase1_cursor_first_boot(cursor_path=self.cursor_path))

        watcher_tick(str(self.cursor_path))
        self.assertEqual(1, self.durable()["generation"])
        self.assertEqual(INIT_COMMITTED, self.state())
        watcher_tick(str(self.cursor_path))
        self.assertEqual(2, self.durable()["generation"])

    def test_crash_after_create_before_commit_adopts_generation_1(self):
        self.write_state(INIT_PREPARED)
        self.seed(1, records={"only": 1})
        saved = self.save({"project_cursor": 1}, expected_generation=1)
        self.assertEqual(2, saved["generation"])
        self.assertEqual({"only": 1}, saved["per_project_record_cursor"])
        self.assertEqual(INIT_COMMITTED, self.state())

    def test_committed_record_without_cursor_is_recovery_required(self):
        self.write_state(INIT_COMMITTED)
        with self.assertRaises(CursorRecoveryRequiredError):
            require_phase1_cursor_first_boot(cursor_path=self.cursor_path)
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 0}, expected_generation=5)
        self.assertFalse(self.cursor_path.exists())

    def test_prepared_and_committed_are_distinguished(self):
        self.write_state(INIT_PREPARED)
        self.assertEqual(INIT_PREPARED, self.state())
        self.assertTrue(require_phase1_cursor_first_boot(cursor_path=self.cursor_path))
        self.write_state(INIT_COMMITTED)
        with self.assertRaises(CursorRecoveryRequiredError):
            require_phase1_cursor_first_boot(cursor_path=self.cursor_path)


class TestLegacyMarkerMigration(RecoveryTestCase):
    """Renaming the artifact must not amount to forgetting what it recorded.

    Round 2 wrote an existence-only ``phase1-cursor.json.initialized``.
    Round 3 renamed it. A deployment that ran round 2 and then lost its
    cursor would show "no record, no cursor" under the new name and be
    reinitialized from zero -- reintroducing the exact P0 both rounds
    exist to close.
    """

    def legacy(self):
        return self.runtime / ("phase1-cursor.json" + pc.LEGACY_INIT_MARKER_SUFFIX)

    def write_legacy(self, body=None):
        self.legacy().write_text(
            json.dumps({"schema": 1, "initialized_at": "2026-09-03T00:00:00Z"} if body is None else body),
            encoding="utf-8")

    def test_legacy_marker_alone_reads_as_committed(self):
        self.write_legacy()
        self.assertEqual(INIT_COMMITTED, self.state())

    def test_lost_cursor_with_only_a_legacy_marker_is_not_reinitialized(self):
        self.write_legacy()
        with self.assertRaises(CursorRecoveryRequiredError):
            require_phase1_cursor_first_boot(cursor_path=self.cursor_path)
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        self.assertFalse(self.cursor_path.exists(),
                         "a lost cursor was rebuilt from zero because the marker had been renamed")

    def test_watcher_tick_does_not_rebuild_behind_a_legacy_marker(self):
        self.write_legacy()
        watcher_tick(str(self.cursor_path))
        watcher_tick(str(self.cursor_path))
        self.assertFalse(self.cursor_path.exists())

    def test_legacy_marker_contents_are_never_parsed(self):
        """Only its presence is evidence; that marker had no trustworthy schema."""
        for body in (b"", b"   ", b"not json at all", b"[]", b'{"schema": 99}'):
            with self.subTest(body=body):
                self.legacy().write_bytes(body)
                self.assertEqual(INIT_COMMITTED, self.state())

    def test_a_live_cursor_beside_a_legacy_marker_still_amends_normally(self):
        self.write_legacy()
        self.seed(2458)
        saved = self.save({"project_cursor": 1}, expected_generation=2458)
        self.assertEqual(2459, saved["generation"])
        self.assertEqual(13, len(saved["per_project_record_cursor"]))
        self.assertEqual(INIT_COMMITTED, self.state())

    def test_the_new_record_wins_over_a_legacy_marker(self):
        self.write_legacy()
        self.write_state(INIT_PREPARED)
        self.assertEqual(INIT_PREPARED, self.state())
        self.assertTrue(require_phase1_cursor_first_boot(cursor_path=self.cursor_path))

    def test_the_legacy_marker_is_never_written_or_removed(self):
        self.write_legacy()
        before = self.legacy().read_bytes()
        self.seed(10)
        self.save({"project_cursor": 1}, expected_generation=10)
        self.assertTrue(self.legacy().exists())
        self.assertEqual(before, self.legacy().read_bytes())
        source = Path(pc.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_write_init_state(_legacy", source)
        writes = [line for line in source.splitlines()
                  if "_legacy_marker_path_for" in line and ("unlink" in line or "write" in line)]
        self.assertEqual([], writes, f"the legacy marker must be read-only: {writes}")


class TestInitStateValidation(RecoveryTestCase):

    BAD = {
        "zero_byte": b"",
        "whitespace": b"  \n",
        "truncated": b'{"schema": 2, "cursor": "phase1-cursor.json", "state": "comm',
        "not_an_object": b"[]",
        "wrong_schema": json.dumps({"schema": 1, "initialized_at": "x"}).encode(),
        "wrong_cursor": json.dumps({"schema": 2, "cursor": "other.json", "state": "committed",
                                    "txid": "t"}).encode(),
        "unknown_state": json.dumps({"schema": 2, "cursor": "phase1-cursor.json", "state": "done",
                                     "txid": "t"}).encode(),
        "no_txid": json.dumps({"schema": 2, "cursor": "phase1-cursor.json", "state": "committed"}).encode(),
    }

    def test_invalid_records_fail_closed_everywhere(self):
        for name, raw in self.BAD.items():
            with self.subTest(record=name):
                self.state_path.write_bytes(raw)
                with self.assertRaises(CursorInitStateError):
                    phase1_cursor_init_state(cursor_path=self.cursor_path)
                with self.assertRaises(CursorInitStateError):
                    require_phase1_cursor_first_boot(cursor_path=self.cursor_path)
                with self.assertRaises(CursorInitStateError):
                    self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
                self.assertFalse(self.cursor_path.exists(), name)
                self.seed(2458)
                before = self.cursor_path.read_bytes()
                with self.assertRaises(CursorInitStateError):
                    self.save({"project_cursor": 1}, expected_generation=2458)
                self.assertEqual(before, self.cursor_path.read_bytes(), name)
                self.assertEqual(raw, self.state_path.read_bytes(), "the bad record was rewritten")
                self.cursor_path.unlink()

    def test_invalid_record_is_a_state_error_not_absence_or_recovery(self):
        self.assertTrue(issubclass(CursorInitStateError, CursorStateError))
        self.assertFalse(issubclass(CursorInitStateError, CursorRecoveryRequiredError))

    def test_watcher_does_not_persist_over_a_corrupt_record(self):
        self.seed(2458)
        self.state_path.write_bytes(b"")
        watcher_tick(str(self.cursor_path))
        self.assertEqual(2458, self.durable()["generation"])
        self.assertEqual(b"", self.state_path.read_bytes())
        self.cursor_path.unlink()
        watcher_tick(str(self.cursor_path))
        self.assertFalse(self.cursor_path.exists(), "a corrupt record was treated as 'never initialized'")


# ---------------------------------------------------------------------------
# Blocker 3 -- no-overwrite publication
# ---------------------------------------------------------------------------


class TestNoOverwritePublication(RecoveryTestCase):

    def install_at_publish(self, generation=2500):
        """Fire at the publish instruction: destination is the canonical name and it is vacant."""
        real = {"replace": os.replace, "rename": os.rename, "link": os.link}
        canonical = os.path.normcase(str(self.cursor_path))
        fired = []

        def hook(name):
            def wrapped(src, dst, *args, **kwargs):
                if not fired and os.path.normcase(str(dst)) == canonical and not os.path.lexists(str(self.cursor_path)):
                    fired.append(1)
                    self.seed(generation, records={"external": 1})
                return real[name](src, dst, *args, **kwargs)
            return wrapped

        return patch.multiple(pc.os, replace=hook("replace"), rename=hook("rename"), link=hook("link")), fired

    def test_external_insert_after_the_last_check_survives(self):
        self.seed(2458)
        ctx, fired = self.install_at_publish()
        with ctx:
            with self.assertRaises(StaleCursorError):
                self.save({"project_cursor": 1}, expected_generation=2458)
        self.assertTrue(fired)
        self.assertEqual(2500, self.durable()["generation"], "the external winner was clobbered")
        claims = self.claims()
        self.assertEqual(1, len(claims), "the claim must be kept for adjudication, not deleted")
        self.assertEqual(2458, self.durable(claims[0])["generation"])

    def test_superseded_claim_is_retired_by_the_next_mutation(self):
        self.seed(2458)
        ctx, _ = self.install_at_publish()
        with ctx:
            with self.assertRaises(StaleCursorError):
                self.save({"project_cursor": 1}, expected_generation=2458)
        saved = self.save({"project_cursor": 1}, expected_generation=2500)
        self.assertEqual(2501, saved["generation"])
        self.assertEqual([], self.claims())

    def test_no_unconditional_replace_over_the_canonical_name(self):
        """Static: the only os.replace destinations are claims and the state record."""
        source = Path(pc.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("os.replace("):
                self.assertTrue("claim" in stripped or "record_path" in stripped,
                                f"unconditional replace found: {stripped}")

    def test_publish_primitive_refuses_an_occupied_destination(self):
        source = self.runtime / "candidate"
        source.write_text("new", encoding="utf-8")
        self.cursor_path.write_text("occupied", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            pc._publish_exclusively(source, self.cursor_path)
        self.assertEqual("occupied", self.cursor_path.read_text(encoding="utf-8"))
        self.assertTrue(source.exists(), "the source must survive a refused publish")
        self.cursor_path.unlink()
        pc._publish_exclusively(source, self.cursor_path)
        self.assertEqual("new", self.cursor_path.read_text(encoding="utf-8"))
        self.assertFalse(source.exists())


# ---------------------------------------------------------------------------
# Claim lifecycle / recovery matrix
# ---------------------------------------------------------------------------


class TestRecoveryMatrix(RecoveryTestCase):

    def claim(self, generation, name="a"):
        path = self.runtime / ("phase1-cursor.json" + CLAIM_INFIX + name)
        self.seed(generation, path=path)
        return path

    def test_cursor_present_no_claim_committed_is_normal(self):
        self.seed(10)
        self.write_state(INIT_COMMITTED)
        self.assertEqual(11, self.save({"project_cursor": 1}, expected_generation=10)["generation"])

    def test_cursor_present_with_older_claim_retires_it(self):
        self.seed(10)
        self.claim(9)
        self.assertEqual(11, self.save({"project_cursor": 1}, expected_generation=10)["generation"])
        self.assertEqual([], self.claims())

    def test_cursor_present_with_newer_claim_fails_closed_and_deletes_nothing(self):
        self.seed(10)
        claim = self.claim(2458)
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 1}, expected_generation=10)
        self.assertEqual(10, self.durable()["generation"])
        self.assertEqual(2458, self.durable(claim)["generation"])

    def test_cursor_present_with_equal_but_different_claim_fails_closed(self):
        self.seed(10)
        claim = self.claim(10)
        claim.write_text(claim.read_text(encoding="utf-8").replace('"project_cursor": 0', '"project_cursor": 7'),
                         encoding="utf-8")
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 1}, expected_generation=10)
        self.assertTrue(claim.exists())

    def test_cursor_present_with_identical_claim_is_a_duplicate_and_retires(self):
        self.seed(10)
        claim = self.runtime / ("phase1-cursor.json" + CLAIM_INFIX + "dup")
        shutil.copyfile(self.cursor_path, claim)
        self.assertEqual(11, self.save({"project_cursor": 1}, expected_generation=10)["generation"])
        self.assertEqual([], self.claims())

    def test_cursor_present_with_unreadable_claim_fails_closed(self):
        self.seed(10)
        claim = self.runtime / ("phase1-cursor.json" + CLAIM_INFIX + "bad")
        claim.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 1}, expected_generation=10)
        self.assertTrue(claim.exists())

    def test_cursor_absent_one_claim_is_recovery_required(self):
        claim = self.claim(2458)
        for token in (CREATE_ONLY, 2458, 0):
            with self.subTest(token=token):
                with self.assertRaises(CursorRecoveryRequiredError):
                    self.save({"project_cursor": 0}, expected_generation=token)
        self.assertFalse(self.cursor_path.exists())
        self.assertEqual(2458, self.durable(claim)["generation"])

    def test_cursor_absent_multiple_claims_fails_closed(self):
        a, b = self.claim(2458, "a"), self.claim(2459, "b")
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 0}, expected_generation=2459)
        self.assertTrue(a.exists() and b.exists())
        self.assertFalse(self.cursor_path.exists())

    def test_cursor_absent_no_claim_committed_fails_closed(self):
        self.write_state(INIT_COMMITTED)
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)

    def test_cursor_absent_no_claim_prepared_resumes_first_boot(self):
        self.write_state(INIT_PREPARED)
        self.assertEqual(1, self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)["generation"])
        self.assertEqual(INIT_COMMITTED, self.state())

    def test_claim_destroyed_during_custody_aborts_without_recreating(self):
        self.seed(2458)
        real = pc._fingerprint

        def destroy(path):
            if CLAIM_INFIX in path.name and path.exists():
                path.unlink()
            return real(path)

        with patch.object(pc, "_fingerprint", side_effect=destroy):
            with self.assertRaises(StaleCursorError):
                self.save({"project_cursor": 1}, expected_generation=2458)
        self.assertFalse(self.cursor_path.exists())
        self.assertEqual(INIT_COMMITTED, self.state(), "the fence must survive the abort")
        self.assertEqual([], [p.name for p in self.runtime.iterdir() if ".candidate-" in p.name])
        watcher_tick(str(self.cursor_path))
        self.assertFalse(self.cursor_path.exists(), "a low-generation cursor was created after the abort")

    def test_stale_candidates_are_debris_and_never_truth(self):
        self.seed(10)
        stale = self.runtime / "phase1-cursor.json.candidate-old"
        self.seed(9999, path=stale)
        self.assertEqual(11, self.save({"project_cursor": 1}, expected_generation=10)["generation"])
        self.assertFalse(stale.exists())


# ---------------------------------------------------------------------------
# Blocker 4 -- the whole Watcher tick binds its path once
# ---------------------------------------------------------------------------


class TestWatcherPathBinding(RecoveryTestCase):

    def setUp(self):
        super().setUp()
        self.original_cwd = os.getcwd()
        self.original_env = dict(os.environ)
        self.a = self.home / "a"
        self.b = self.home / "b"
        self.pa = self.a / "runtime" / "phase1-cursor.json"
        self.pb = self.b / "runtime" / "phase1-cursor.json"
        self.seed(2458, path=self.pa)
        self.seed(2458, path=self.pb)

    def tearDown(self):
        os.chdir(self.original_cwd)
        os.environ.clear()
        os.environ.update(self.original_env)

    def hijack(self, relative_home):
        os.chdir(str(self.b))
        os.environ.update(AI_MANAGER_HOME="." if relative_home else str(self.b),
                          USERPROFILE=str(self.b), HOME=str(self.b))

    def test_relative_cursor_path_survives_a_midway_hijack(self):
        os.chdir(str(self.a))
        os.environ["AI_MANAGER_HOME"] = str(self.a)
        watcher_tick("runtime/phase1-cursor.json", midway=lambda: self.hijack(False))
        self.assertEqual(2459, self.durable(self.pa)["generation"])
        self.assertEqual(2458, self.durable(self.pb)["generation"], "the decoy was advanced")

    def test_relative_manager_home_survives_a_midway_hijack(self):
        os.chdir(str(self.a))
        os.environ["AI_MANAGER_HOME"] = "."
        watcher_tick(None, midway=lambda: self.hijack(True))
        self.assertEqual(2459, self.durable(self.pa)["generation"])
        self.assertEqual(2458, self.durable(self.pb)["generation"], "the decoy was advanced")

    def test_load_and_save_receive_the_identical_bound_path(self):
        import manager.command_watcher as cw
        received = []
        real_load, real_save = pc.load_phase1_cursor, pc.save_phase1_cursor

        def load(**kwargs):
            received.append(("load", kwargs.get("cursor_path")))
            return real_load(**kwargs)

        def save(data, **kwargs):
            received.append(("save", kwargs.get("cursor_path")))
            return real_save(data, **kwargs)

        os.chdir(str(self.a))
        os.environ["AI_MANAGER_HOME"] = "."
        with patch.object(pc, "load_phase1_cursor", side_effect=load), \
                patch.object(pc, "save_phase1_cursor", side_effect=save):
            watcher_tick(None, midway=lambda: self.hijack(True))
        kinds = [k for k, _ in received]
        self.assertEqual(["load", "save"], kinds, received)
        (_, loaded), (_, saved) = received
        self.assertIs(loaded, saved, "load and save must use the same bound object")
        self.assertTrue(loaded.is_absolute())
        self.assertEqual(self.pa.resolve(), loaded)
        self.assertEqual(2459, self.durable(self.pa)["generation"])

    def test_bind_is_stable_under_environment_change(self):
        os.chdir(str(self.a))
        os.environ["AI_MANAGER_HOME"] = "."
        bound = bind_phase1_cursor_path()
        self.hijack(True)
        self.assertEqual(bound, bind_phase1_cursor_path(cursor_path=bound))
        self.assertEqual(self.pa.resolve(), bound)
        self.assertEqual(2458, load_phase1_cursor(cursor_path=bound)["generation"])


if __name__ == "__main__":
    unittest.main()
