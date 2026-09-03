"""Regression suite for Phase-1 cursor generation monotonicity.

Every test here names a route by which the durable cursor could lose
generations or projects. Four of them were real, live in production at
299fbbb, and reproduce verbatim against that revision:

* ``expected_generation=None`` (and omitting it) skipped the CAS;
* the next generation came from the caller's in-memory snapshot, so even
  a correct CAS token still rolled the file backward;
* the loader turned corruption and absence alike into generation 0, so
  the CAS compared 0 against 0 and passed;
* compare and replace were not serialized.

The incident this suite exists to prevent: a durable cursor at
generation 2458 covering 13 projects became generation 6 covering 5
(2026-09-02). The literal 2458 appears below because a regression that
reproduces the real numbers is the one nobody argues with.
"""

import json
import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from manager.manager_home import ManagerHomeError
from manager.phase1_cursor import (
    CREATE_ONLY,
    INIT_MARKER_SUFFIX,
    CursorRecoveryRequiredError,
    CursorContractError,
    CursorLockError,
    CursorMissingError,
    CursorParseError,
    CursorReadError,
    CursorSchemaError,
    CursorStateError,
    StaleCursorError,
    _resolve_cursor_path,
    default_phase1_cursor,
    load_phase1_cursor,
    phase1_cursor_exists,
    save_phase1_cursor,
)

PROD_13_PROJECTS = {f"proj-{i:02d}": 100 + i for i in range(13)}
CALLER_5_PROJECTS = {f"proj-{i:02d}": 1 for i in range(5)}


def fail_canonical_install(cursor_path, only_candidates=False):
    """Fault injection at the OS layer: installing ANY file at the canonical
    cursor name fails, whichever primitive (link, rename, replace) the code
    reaches for. Custody renames (destination is a claim) still work. With
    ``only_candidates`` the restore of a claim still works too, modelling a
    full disk rather than a filesystem that has stopped working."""
    import manager.phase1_cursor as pc
    canonical = os.path.normcase(str(cursor_path))
    real = {"replace": os.replace, "rename": os.rename, "link": os.link}

    def guard(name):
        def wrapped(src, dst, *args, **kwargs):
            if os.path.normcase(str(dst)) == canonical and (
                    not only_candidates or ".candidate-" in str(src)):
                raise OSError(f"injected: {name} to the canonical cursor name refused")
            return real[name](src, dst, *args, **kwargs)
        return wrapped

    return patch.multiple(pc.os, replace=guard("replace"), rename=guard("rename"), link=guard("link"))


class CursorTestCase(unittest.TestCase):
    """A throwaway manager home per test. Never the production one."""

    def setUp(self):
        import tempfile
        self.home = Path(tempfile.mkdtemp(prefix="adm-cursor-integrity-"))
        self.runtime = self.home / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.cursor_path = self.runtime / "phase1-cursor.json"
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)

    def seed(self, generation, records=None, visits=None, project_cursor=0):
        """Write a durable cursor directly, bypassing the API under test."""
        self.cursor_path.write_text(json.dumps({
            "project_cursor": project_cursor,
            "per_project_record_cursor": records if records is not None else {},
            "per_project_attention_visits": visits if visits is not None else {},
            "generation": generation,
            "updated_at": "2026-09-02T23:31:20Z",
        }, indent=2), encoding="utf-8")

    def durable(self):
        return json.loads(self.cursor_path.read_text(encoding="utf-8"))

    def save(self, cursor_data, **kwargs):
        return save_phase1_cursor(cursor_data, cursor_path=self.cursor_path, **kwargs)


class TestCompareAndSwap(CursorTestCase):
    """Required tests 1, 2, 5."""

    def test_1_matching_expected_generation_advances_by_one(self):
        self.seed(10)
        saved = self.save({"project_cursor": 3, "generation": 10}, expected_generation=10)
        self.assertEqual(11, saved["generation"])
        self.assertEqual(11, self.durable()["generation"])

    def test_2_stale_expected_generation_is_rejected(self):
        self.seed(10)
        with self.assertRaises(StaleCursorError):
            self.save({"project_cursor": 3, "generation": 9}, expected_generation=9)
        self.assertEqual(10, self.durable()["generation"],
                         "a rejected CAS must not have touched the durable cursor")

    def test_5_next_generation_derives_from_durable_state_not_caller(self):
        """The caller's generation field is inert. Only the file decides."""
        self.seed(400)
        for caller_generation in (0, 1, 5, 399, 10_000):
            with self.subTest(caller_generation=caller_generation):
                self.seed(400)
                saved = self.save({"project_cursor": 1, "generation": caller_generation},
                                  expected_generation=400)
                self.assertEqual(401, saved["generation"])
                self.assertEqual(401, self.durable()["generation"])


class TestTheIncidentSignature(CursorTestCase):
    """Required tests 3 and 4 -- the 2458 -> 6 collapse, both routes."""

    def test_3_no_cas_token_cannot_overwrite_an_existing_cursor(self):
        """PRE-FIX this produced generation 6. It must now be impossible."""
        for kwargs in ({}, {"expected_generation": None}):
            with self.subTest(kwargs=kwargs or "omitted"):
                self.seed(2458, records=dict(PROD_13_PROJECTS))
                with self.assertRaises(CursorContractError):
                    self.save({"project_cursor": 0,
                               "per_project_record_cursor": dict(CALLER_5_PROJECTS),
                               "generation": 5}, **kwargs)
                after = self.durable()
                self.assertEqual(2458, after["generation"],
                                 "durable generation must be untouched")
                self.assertEqual(13, len(after["per_project_record_cursor"]),
                                 "durable project coverage must be untouched")

    def test_4_correct_cas_token_with_stale_caller_generation_yields_2459(self):
        """PRE-FIX this ALSO produced 6, because the caller supplied the generation."""
        self.seed(2458, records=dict(PROD_13_PROJECTS))
        saved = self.save({"project_cursor": 0,
                           "per_project_record_cursor": dict(CALLER_5_PROJECTS),
                           "generation": 5},
                          expected_generation=2458)
        self.assertEqual(2459, saved["generation"])
        self.assertNotEqual(6, saved["generation"])
        self.assertEqual(2459, self.durable()["generation"])

    def test_4b_partial_caller_snapshot_does_not_drop_projects(self):
        """13 projects -> 5 was half the incident. Merge, never replace."""
        self.seed(2458, records=dict(PROD_13_PROJECTS))
        self.save({"project_cursor": 0,
                   "per_project_record_cursor": {"proj-00": 999},
                   "generation": 5},
                  expected_generation=2458)
        after = self.durable()
        self.assertEqual(13, len(after["per_project_record_cursor"]))
        self.assertEqual(999, after["per_project_record_cursor"]["proj-00"],
                         "the project the caller knew about still advances")
        self.assertEqual(112, after["per_project_record_cursor"]["proj-12"],
                         "the twelve it did not know about are preserved")


class TestContract(CursorTestCase):
    """The footgun is gone and cannot be reached by any argument value."""

    def test_expected_generation_none_is_refused_even_with_no_cursor(self):
        with self.assertRaises(CursorContractError):
            self.save({"project_cursor": 0, "generation": 0}, expected_generation=None)
        self.assertFalse(self.cursor_path.exists())

    def test_nonsense_cas_tokens_are_refused(self):
        self.seed(10)
        for bad in (-1, True, False, "10", 10.0, object()):
            with self.subTest(bad=bad):
                with self.assertRaises(CursorContractError):
                    self.save({"generation": 10}, expected_generation=bad)
        self.assertEqual(10, self.durable()["generation"])


class TestStrictLoader(CursorTestCase):
    """Required tests 7, 8, 9, 11 -- absence and corruption are different."""

    def test_7_mutation_reread_never_falls_back_to_a_default(self):
        """A corrupt file must not present itself to the CAS as generation 0."""
        self.cursor_path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(CursorParseError):
            load_phase1_cursor(cursor_path=self.cursor_path)
        with self.assertRaises(CursorParseError):
            load_phase1_cursor(cursor_path=self.cursor_path, missing_ok=True)
        # missing_ok covers absence only; it never softens corruption.
        with self.assertRaises(CursorParseError):
            self.save({"generation": 0}, expected_generation=0)
        self.assertEqual("{ not json", self.cursor_path.read_text(encoding="utf-8"),
                         "the untrustworthy file is left intact as evidence")

    def test_8_malformed_json_fails_closed(self):
        self.seed(2458, records=dict(PROD_13_PROJECTS))
        self.cursor_path.write_text('["a", "list", "not", "an", "object"]', encoding="utf-8")
        with self.assertRaises(CursorSchemaError):
            self.save({"generation": 0}, expected_generation=0)

    def test_8b_schema_violations_fail_closed(self):
        cases = {
            "generation_missing": {"project_cursor": 0},
            "generation_negative": {"project_cursor": 0, "generation": -1},
            "generation_string": {"project_cursor": 0, "generation": "2458"},
            "generation_bool": {"project_cursor": 0, "generation": True},
            "records_not_a_map": {"project_cursor": 0, "generation": 5,
                                  "per_project_record_cursor": [1, 2]},
            "records_bad_value": {"project_cursor": 0, "generation": 5,
                                  "per_project_record_cursor": {"p": "nope"}},
            "project_cursor_negative": {"project_cursor": -3, "generation": 5},
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                self.cursor_path.write_text(json.dumps(body), encoding="utf-8")
                with self.assertRaises(CursorSchemaError):
                    load_phase1_cursor(cursor_path=self.cursor_path)

    def test_9_unreadable_file_fails_closed(self):
        self.seed(2458)
        with patch.object(Path, "read_text", side_effect=OSError("device is not ready")):
            with self.assertRaises(CursorReadError):
                load_phase1_cursor(cursor_path=self.cursor_path)
            with self.assertRaises(CursorReadError):
                self.save({"generation": 0}, expected_generation=0)
        self.assertEqual(2458, self.durable()["generation"])

    def test_9b_undecodable_bytes_fail_closed(self):
        self.cursor_path.write_bytes(b"\xff\xfe\x00 not utf-8 at all")
        with self.assertRaises(CursorParseError):
            load_phase1_cursor(cursor_path=self.cursor_path)

    def test_11_previously_existing_cursor_gone_is_not_reinitialized(self):
        """The Watcher's exact shape: load, then save on what load reported."""
        self.seed(2458, records=dict(PROD_13_PROJECTS))
        self.cursor_path.unlink()
        # A tolerant read still reports the default -- that is allowed for
        # observation -- but it may not be laundered into a write.
        observed = load_phase1_cursor(cursor_path=self.cursor_path)
        self.assertEqual(0, observed["generation"])
        with self.assertRaises(StaleCursorError):
            self.save({"project_cursor": 0, "generation": 0}, expected_generation=0)
        self.assertFalse(self.cursor_path.exists(),
                         "a failed CAS must not have created a generation-1 cursor")

    def test_11b_strict_load_distinguishes_absent_from_corrupt(self):
        with self.assertRaises(CursorMissingError):
            load_phase1_cursor(cursor_path=self.cursor_path, missing_ok=False)
        self.assertNotIsInstance(
            CursorMissingError("x"), CursorStateError,
            "absence must not be catchable as corruption, nor the reverse")
        self.cursor_path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(CursorStateError):
            load_phase1_cursor(cursor_path=self.cursor_path, missing_ok=False)


class TestInitialization(CursorTestCase):
    """Required test 12 -- first-ever boot is explicit, and only that."""

    def test_12_create_only_bootstraps_a_missing_cursor(self):
        self.assertFalse(phase1_cursor_exists(cursor_path=self.cursor_path))
        saved = self.save({"project_cursor": 0, "per_project_record_cursor": {"p1": 0}},
                          expected_generation=CREATE_ONLY)
        self.assertEqual(1, saved["generation"])
        self.assertEqual(1, self.durable()["generation"])
        self.assertTrue(phase1_cursor_exists(cursor_path=self.cursor_path))

    def test_12b_create_only_refuses_to_clobber_an_existing_cursor(self):
        self.seed(2458, records=dict(PROD_13_PROJECTS))
        with self.assertRaises(StaleCursorError):
            self.save({"project_cursor": 0,
                       "per_project_record_cursor": dict(CALLER_5_PROJECTS)},
                      expected_generation=CREATE_ONLY)
        after = self.durable()
        self.assertEqual(2458, after["generation"])
        self.assertEqual(13, len(after["per_project_record_cursor"]))

    def test_12c_create_only_refuses_when_the_cursor_is_corrupt(self):
        """Corruption is not absence, so it is not a bootstrap opportunity."""
        self.cursor_path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(StaleCursorError):
            self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        self.assertEqual("{ not json", self.cursor_path.read_text(encoding="utf-8"))


class TestPathBinding(CursorTestCase):
    """Required test 6 -- one operation, one path, resolved once."""

    def test_6_path_is_resolved_exactly_once_per_operation(self):
        self.seed(10)
        real = _resolve_cursor_path
        calls = []

        def counting(**kwargs):
            calls.append(kwargs)
            return real(**kwargs)

        with patch("manager.phase1_cursor._resolve_cursor_path", side_effect=counting):
            save_phase1_cursor({"project_cursor": 1, "generation": 10},
                               cursor_path=self.cursor_path, expected_generation=10)
        self.assertEqual(1, len(calls),
                         f"one save must resolve the cursor path once, resolved {len(calls)} times")

    def test_6b_operation_stays_bound_when_resolution_changes_underneath(self):
        """A manager home that moves mid-write must not split read from replace."""
        other = self.runtime / "elsewhere-phase1-cursor.json"
        self.seed(10, records={"kept": 7})
        resolutions = iter([self.cursor_path, other, other, other, other])

        with patch("manager.phase1_cursor._resolve_cursor_path",
                   side_effect=lambda **kw: next(resolutions)):
            saved = save_phase1_cursor({"project_cursor": 1, "generation": 10},
                                       expected_generation=10)

        self.assertEqual(11, saved["generation"])
        self.assertEqual(11, self.durable()["generation"],
                         "the write landed on the path the read was bound to")
        self.assertFalse(other.exists(),
                         "no part of the operation followed the changed resolution")
        self.assertEqual({"kept": 7}, self.durable()["per_project_record_cursor"])

    def test_17_resolver_contract_from_3253cf2_is_preserved(self):
        """Runtime-home consolidation must survive this change untouched.

        The contract 3253cf2 established is "explicit > AI_MANAGER_HOME >
        canonical user-level home, and never the working directory". A
        blank ``AI_MANAGER_HOME`` therefore falls through to the canonical
        home, which is correct -- the P0 was the cwd fallback, not this.
        """
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ManagerHomeError):
                _resolve_cursor_path()

        with patch.dict(os.environ, {"AI_MANAGER_HOME": "", "USERPROFILE": str(self.home)}):
            resolved = _resolve_cursor_path()
        self.assertEqual((self.home / ".ai-development-manager" / "runtime"
                          / "phase1-cursor.json").resolve(), resolved.resolve(),
                         "a blank home falls through to the canonical user-level home")
        self.assertNotEqual(Path.cwd().resolve(), Path(resolved).parent.parent.resolve(),
                            "and never to the working directory")

        with patch.dict(os.environ, {"AI_MANAGER_HOME": str(self.home)}):
            self.assertEqual(self.cursor_path.resolve(), _resolve_cursor_path().resolve())

    def test_17c_a_manager_home_inside_a_checkout_is_still_rejected(self):
        """GIT_WORKTREE_HOME_REJECTED -- the 2026-09-02 outage guard."""
        checkout = self.home / "fake-checkout"
        (checkout / ".git").mkdir(parents=True)
        with patch.dict(os.environ, {"AI_MANAGER_HOME": str(checkout)}):
            with self.assertRaises(ManagerHomeError):
                _resolve_cursor_path()

    def test_17b_lock_file_lands_beside_the_cursor_under_the_manager_home(self):
        """The new lock must not become a second thing written somewhere else."""
        before = {p.name for p in self.runtime.iterdir()}
        self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        created = {p.name for p in self.runtime.iterdir()} - before
        self.assertTrue(created.issubset({"phase1-cursor.json", "phase1-cursor.json.lock",
                                          "phase1-cursor.json" + INIT_MARKER_SUFFIX}),
                        f"unexpected files written: {created}")


class TestMutationRaces(CursorTestCase):
    """Required tests 10, 13, 14."""

    def test_10_cursor_vanishing_mid_write_fails_closed(self):
        """Deleted between the CAS read and the replace -> refuse, do not recreate."""
        self.seed(2458, records=dict(PROD_13_PROJECTS))

        def vanish():
            if self.cursor_path.exists():
                self.cursor_path.unlink()
            return "2026-09-03T00:00:00Z"

        with patch("manager.phase1_cursor.now_iso", side_effect=vanish):
            with self.assertRaises(StaleCursorError):
                self.save({"project_cursor": 0, "generation": 2458}, expected_generation=2458)
        self.assertFalse(self.cursor_path.exists(),
                         "a vanished cursor must not be resurrected at generation 2459")

    def test_10b_cursor_changing_mid_write_fails_closed(self):
        self.seed(2458)

        def advance():
            self.seed(2500)
            return "2026-09-03T00:00:00Z"

        with patch("manager.phase1_cursor.now_iso", side_effect=advance):
            with self.assertRaises(StaleCursorError):
                self.save({"project_cursor": 0, "generation": 2458}, expected_generation=2458)
        self.assertEqual(2500, self.durable()["generation"],
                         "the newer writer's state survives intact")

    def test_13_concurrent_writers_on_the_same_generation_have_one_winner(self):
        self.seed(10)
        writers = 8
        barrier = threading.Barrier(writers)
        outcomes = []
        lock = threading.Lock()

        def writer(n):
            barrier.wait()
            try:
                self.save({"project_cursor": n, "generation": 10}, expected_generation=10)
                result = "won"
            except StaleCursorError:
                result = "lost"
            except CursorLockError:
                result = "lock-timeout"
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(1, outcomes.count("won"),
                         f"exactly one writer may win, got {outcomes}")
        self.assertEqual(writers - 1, outcomes.count("lost"), outcomes)
        self.assertEqual(11, self.durable()["generation"],
                         "one winner means exactly one generation advance")

    def test_13b_concurrent_writers_never_lower_the_generation(self):
        """Monotonicity holds under contention even where serialization does not."""
        self.seed(100)
        observed = []
        lock = threading.Lock()
        barrier = threading.Barrier(6)

        def writer():
            barrier.wait()
            for _ in range(5):
                try:
                    current = load_phase1_cursor(cursor_path=self.cursor_path)
                    self.save({"project_cursor": 0, "generation": current["generation"]},
                              expected_generation=current["generation"])
                except (StaleCursorError, CursorLockError):
                    pass
                with lock:
                    observed.append(load_phase1_cursor(cursor_path=self.cursor_path)["generation"])

        threads = [threading.Thread(target=writer) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertTrue(all(g >= 100 for g in observed),
                        f"generation dropped below the seeded 100: {min(observed)}")
        self.assertEqual(sorted(observed), observed,
                         "generation must never decrease over the run")

    def test_14_write_is_atomic_and_leaves_no_debris(self):
        """One custody rename, one no-overwrite publish, and nothing unconditional over the cursor."""
        self.seed(10)
        replaced = []
        real_replace = os.replace

        def recording(src, dst):
            replaced.append((str(src), str(dst)))
            return real_replace(src, dst)

        with patch("manager.phase1_cursor.os.replace", side_effect=recording):
            self.save({"project_cursor": 1, "generation": 10}, expected_generation=10)

        custody = [(s, d) for s, d in replaced if s == str(self.cursor_path)]
        self.assertEqual(1, len(custody), f"expected exactly one custody rename, got {replaced}")
        self.assertIn(".claim-", custody[0][1])
        self.assertEqual([], [(s, d) for s, d in replaced if d == str(self.cursor_path)],
                         "nothing may os.replace over the canonical cursor name")
        self.assertEqual(11, self.durable()["generation"])
        leftovers = [p.name for p in self.runtime.iterdir()
                     if p.name not in ("phase1-cursor.json", "phase1-cursor.json.lock",
                                       "phase1-cursor.json" + INIT_MARKER_SUFFIX)]
        self.assertEqual([], leftovers, f"temp or claim debris left behind: {leftovers}")

    def test_14b_a_failed_swap_leaves_the_durable_cursor_untouched(self):
        """The custody rename must be undone if publication cannot complete."""
        self.seed(2458, records=dict(PROD_13_PROJECTS))
        before = self.cursor_path.read_bytes()

        # Fail installing the NEW file, the way a full disk would. The
        # custody rename and the restore move an existing file and
        # allocate nothing, so they still work -- failing those too would
        # be modelling a filesystem that has stopped working altogether,
        # which is the next test.
        with fail_canonical_install(self.cursor_path, only_candidates=True):
            with self.assertRaises(OSError):
                self.save({"project_cursor": 0, "generation": 2458}, expected_generation=2458)
        self.assertTrue(self.cursor_path.exists(),
                        "the durable cursor was left in custody instead of restored")
        self.assertEqual(before, self.cursor_path.read_bytes())
        leftovers = [p.name for p in self.runtime.iterdir()
                     if p.name not in ("phase1-cursor.json", "phase1-cursor.json.lock",
                                       "phase1-cursor.json" + INIT_MARKER_SUFFIX)]
        self.assertEqual([], leftovers, f"temp or claim debris left behind: {leftovers}")

    def test_if_even_the_restore_fails_the_state_survives_under_the_claim(self):
        """Worst case: the durable bytes are preserved and the runtime fails closed.

        If publication AND the restore both fail, the cursor is not at its
        canonical name any more. That is bad, but it is the safe kind of
        bad: the bytes still exist under the claim name for a human to
        recover, and any existing claim means the next tick refuses to
        reinitialise from zero rather than inventing a generation-1 cursor.
        """
        self.seed(2458, records=dict(PROD_13_PROJECTS))
        self.save({"project_cursor": 0, "generation": 2458}, expected_generation=2458)
        before = self.cursor_path.read_bytes()

        with fail_canonical_install(self.cursor_path):
            with self.assertRaises(OSError):
                self.save({"project_cursor": 0, "generation": 2459}, expected_generation=2459)

        claims = [p for p in self.runtime.iterdir() if ".claim-" in p.name]
        self.assertEqual(1, len(claims), "the durable bytes were lost entirely")
        self.assertEqual(before, claims[0].read_bytes(), "the preserved bytes are not the cursor")
        self.assertFalse(self.cursor_path.exists())
        # And the next tick refuses to start over.
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)


class TestWatcherCaller(CursorTestCase):
    """Required tests 16 and 18 -- the one production mutation caller."""

    def test_16_watcher_presents_a_cas_token_and_never_the_footgun(self):
        source = Path(__file__).with_name("command_watcher.py").read_text(encoding="utf-8")
        self.assertNotIn("expected_generation=None", source)
        self.assertNotIn("expected_generation=current_gen", source)
        self.assertIn("expected_generation=cursor_cas_token", source)
        saves = [line for line in source.splitlines() if "save_phase1_cursor(" in line
                 and "import" not in line]
        self.assertEqual(1, len(saves), f"expected exactly one save call site, found {saves}")

    def test_16b_watcher_binds_a_token_for_each_of_the_three_states(self):
        """Absent -> CREATE_ONLY, present -> generation, corrupt -> no write."""
        source = Path(__file__).with_name("command_watcher.py").read_text(encoding="utf-8")
        self.assertIn("except CursorMissingError:", source)
        self.assertIn("cursor_cas_token = CREATE_ONLY", source)
        self.assertIn("except CursorStateError as exc:", source)
        self.assertIn("cursor_cas_token = None", source)
        self.assertIn("missing_ok=False", source)

    def test_18_repeated_reset_like_ticks_cannot_rebuild_from_zero(self):
        """Many ticks that each believe they are starting fresh change nothing."""
        self.seed(2458, records=dict(PROD_13_PROJECTS))
        for tick in range(25):
            with self.subTest(tick=tick):
                # A tick holding a default, zeroed snapshot -- the exact
                # state the old tolerant loader handed out after a failed
                # read -- has no route to the durable file.
                blank = default_phase1_cursor()
                with self.assertRaises((CursorContractError, StaleCursorError)):
                    self.save(blank, expected_generation=blank["generation"])
                with self.assertRaises(CursorContractError):
                    self.save(blank)
                with self.assertRaises(StaleCursorError):
                    self.save(blank, expected_generation=CREATE_ONLY)
        after = self.durable()
        self.assertEqual(2458, after["generation"])
        self.assertEqual(13, len(after["per_project_record_cursor"]))

    def test_18b_generation_is_monotonic_across_a_long_legitimate_run(self):
        self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        previous = 1
        for _ in range(50):
            current = load_phase1_cursor(cursor_path=self.cursor_path)
            saved = self.save({"project_cursor": current["project_cursor"] + 1,
                               "generation": 0},  # deliberately wrong; must be ignored
                              expected_generation=current["generation"])
            self.assertEqual(previous + 1, saved["generation"])
            previous = saved["generation"]
        self.assertEqual(51, self.durable()["generation"])


class TestInitializationFence(CursorTestCase):
    """Round 2, blocker 1: absence and never-having-existed are different.

    An independent review deleted a generation-2459 cursor covering 13
    projects and watched the next Watcher tick recreate it at generation 1
    covering 1. CREATE_ONLY only knew "no file right now", which a
    deletion satisfies just as well as a first boot does. The cursor file
    cannot answer the question -- its own absence is the thing being
    diagnosed -- so a separate durable marker does.
    """

    def marker(self):
        return self.cursor_path.with_name(self.cursor_path.name + INIT_MARKER_SUFFIX)

    def test_first_ever_initialization_still_works(self):
        self.assertFalse(self.marker().exists())
        saved = self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        self.assertEqual(1, saved["generation"])
        self.assertTrue(self.marker().exists(), "creating a cursor must record that it happened")

    def test_marker_survives_deletion_of_the_cursor(self):
        self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        self.cursor_path.unlink()
        self.assertTrue(self.marker().exists(),
                        "deleting the cursor must not erase initialization history")

    def test_create_only_refuses_after_prior_existence(self):
        self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        self.save({"project_cursor": 1}, expected_generation=1)
        self.assertEqual(2, self.durable()["generation"])
        self.cursor_path.unlink()
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        self.assertFalse(self.cursor_path.exists(),
                         "a lost cursor must not be silently reinitialized")

    def test_recovery_required_is_not_catchable_as_corruption_or_absence(self):
        """Nothing may handle a lost cursor by handling something milder."""
        self.assertFalse(issubclass(CursorRecoveryRequiredError, CursorStateError))
        self.assertFalse(issubclass(CursorRecoveryRequiredError, CursorMissingError))
        self.assertFalse(issubclass(CursorMissingError, CursorRecoveryRequiredError))

    def test_an_existing_unmarked_cursor_is_fenced_on_its_first_write(self):
        """The production cursor predates the marker; its next tick fences it."""
        self.seed(2458, records=dict(PROD_13_PROJECTS))
        self.assertFalse(self.marker().exists())
        self.save({"project_cursor": 1}, expected_generation=2458)
        self.assertTrue(self.marker().exists())
        self.cursor_path.unlink()
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)

    def test_marker_carries_no_cursor_authority(self):
        """It is provenance, not a second cursor."""
        self.save({"project_cursor": 5, "per_project_record_cursor": {"p": 9}},
                  expected_generation=CREATE_ONLY)
        body = json.loads(self.marker().read_text(encoding="utf-8"))
        self.assertEqual({"schema", "cursor", "state", "txid", "recorded_at"}, set(body))
        self.assertEqual("committed", body["state"])
        self.assertEqual(self.cursor_path.name, body["cursor"])
        for forbidden in ("generation", "project_cursor", "per_project_record_cursor",
                          "per_project_attention_visits"):
            self.assertNotIn(forbidden, body)

    def test_watcher_tick_does_not_rebuild_a_lost_cursor(self):
        """The end-to-end scenario, through the real poll_once."""
        from manager.command_watcher import poll_once
        from manager.test_phase1_fair_scheduling import MemoryDiscoveryStore
        from manager.trusted_ingress import TRUSTED_INGRESS_ORIGIN

        def tasks(pid):
            return [{"project_id": pid, "task_id": "%s-t%d" % (pid, i), "title": "T",
                     "status": "queued", "recommended_provider": None,
                     "quota_evidence": {"codex": {}},
                     "source_context": {"origin": TRUSTED_INGRESS_ORIGIN}} for i in range(6)]

        def tick():
            store = MemoryDiscoveryStore({p: tasks(p) for p in ("p0", "p1")})
            with patch("manager.command_watcher.read_drive_status",
                       return_value={"codex": {"status": "available"}}):
                poll_once(store, None, discovery_store=store,
                          cursor_path=str(self.cursor_path), allowlist=frozenset())

        tick()
        tick()
        self.assertEqual(2, self.durable()["generation"])
        self.cursor_path.unlink()
        tick()
        self.assertFalse(self.cursor_path.exists(),
                         "the Watcher recreated a lost cursor from zero")


class TestSwapLinearization(CursorTestCase):
    """Round 2, blocker 2: the last check must be adjacent to the swap.

    An independent review let the target pass its final verification, then
    installed generation 2500, and watched generation 2459 replace it.
    Verifying and then replacing is not a compare-and-swap; the durable
    file has to be taken into custody so the verification describes
    something no competitor can still reach by name.
    """

    def test_competitor_landing_after_the_final_check_is_not_clobbered(self):
        self.seed(2458, records=dict(PROD_13_PROJECTS))
        import manager.phase1_cursor as pc
        real = pc._fingerprint

        def install_competitor(path):
            fingerprint = real(path)
            if ".claim-" in path.name:
                self.seed(2500, records={"newer": 1})
            return fingerprint

        with patch.object(pc, "_fingerprint", side_effect=install_competitor):
            with self.assertRaises(StaleCursorError):
                self.save({"project_cursor": 0, "generation": 2458}, expected_generation=2458)
        self.assertEqual(2500, self.durable()["generation"],
                         "the newer writer's state was destroyed")

    def test_competitor_landing_just_before_the_swap_is_not_clobbered(self):
        self.seed(2458, records=dict(PROD_13_PROJECTS))
        import manager.phase1_cursor as pc
        real = pc._write_temp

        def install_competitor(path, payload):
            self.seed(2500, records={"newer": 1})
            return real(path, payload)

        with patch.object(pc, "_write_temp", side_effect=install_competitor):
            with self.assertRaises(StaleCursorError):
                self.save({"project_cursor": 0, "generation": 2458}, expected_generation=2458)
        self.assertEqual(2500, self.durable()["generation"])

    def test_destroying_the_file_mid_swap_aborts_without_recreating(self):
        self.seed(2458, records=dict(PROD_13_PROJECTS))
        import manager.phase1_cursor as pc
        real = pc._fingerprint

        def destroy(path):
            if ".claim-" in path.name and path.exists():
                path.unlink()
            return real(path)

        with patch.object(pc, "_fingerprint", side_effect=destroy):
            with self.assertRaises(StaleCursorError):
                self.save({"project_cursor": 0, "generation": 2458}, expected_generation=2458)
        self.assertFalse(self.cursor_path.exists(),
                         "a destroyed cursor must not be resurrected")

    def test_an_aborted_swap_restores_the_durable_file(self):
        """Refusing must leave the state untouched, not merely unwritten."""
        self.seed(2458, records=dict(PROD_13_PROJECTS))
        before = self.cursor_path.read_bytes()
        import manager.phase1_cursor as pc
        genuine = pc._fingerprint(self.cursor_path)
        with patch.object(pc, "_fingerprint", side_effect=[genuine, ("bogus", 0, 0, 0)]):
            with self.assertRaises(StaleCursorError):
                self.save({"project_cursor": 0, "generation": 2458}, expected_generation=2458)
        self.assertEqual(before, self.cursor_path.read_bytes())

    def test_no_claim_debris_after_success(self):
        self.seed(10)
        self.save({"project_cursor": 1, "generation": 10}, expected_generation=10)
        leftovers = [p.name for p in self.runtime.iterdir() if ".claim-" in p.name]
        self.assertEqual([], leftovers, "claim debris left behind: %s" % leftovers)


class TestAbsolutePathBinding(CursorTestCase):
    """Round 2, blocker 3: resolved once is worthless if it resolved to a relative path."""

    def test_relative_cursor_path_is_bound_absolutely(self):
        original = os.getcwd()
        decoy = self.home / "decoy"
        (decoy / "runtime").mkdir(parents=True)
        self.seed(10, records={"kept": 1})
        import manager.phase1_cursor as pc
        real_now = pc.now_iso

        def chdir_away():
            os.chdir(str(decoy))
            return real_now()

        try:
            os.chdir(str(self.home))
            with patch.object(pc, "now_iso", side_effect=chdir_away):
                saved = save_phase1_cursor({"project_cursor": 1, "generation": 10},
                                           cursor_path="runtime/phase1-cursor.json",
                                           expected_generation=10)
        finally:
            os.chdir(original)
        self.assertEqual(11, saved["generation"])
        self.assertEqual(11, self.durable()["generation"])
        self.assertFalse((decoy / "runtime" / "phase1-cursor.json").exists(),
                         "the write followed the working directory instead of its binding")

    def test_relative_manager_home_is_bound_absolutely(self):
        original = os.getcwd()
        decoy = self.home / "decoy2"
        (decoy / "runtime").mkdir(parents=True)
        self.seed(20, records={"kept": 1})
        import manager.phase1_cursor as pc
        real_now = pc.now_iso

        def chdir_away():
            os.chdir(str(decoy))
            os.environ["AI_MANAGER_HOME"] = str(decoy)
            return real_now()

        try:
            os.chdir(str(self.home.parent))
            with patch.dict(os.environ, {"AI_MANAGER_HOME": self.home.name}):
                with patch.object(pc, "now_iso", side_effect=chdir_away):
                    saved = save_phase1_cursor({"project_cursor": 1, "generation": 20},
                                               expected_generation=20)
        finally:
            os.chdir(original)
        self.assertEqual(21, saved["generation"])
        self.assertEqual(21, self.durable()["generation"])
        self.assertFalse((decoy / "runtime" / "phase1-cursor.json").exists())

    def test_resolver_returns_absolute_paths(self):
        with patch.dict(os.environ, {"AI_MANAGER_HOME": str(self.home)}):
            self.assertTrue(_resolve_cursor_path().is_absolute())
        self.assertTrue(_resolve_cursor_path(cursor_path="relative/cursor.json").is_absolute())
        original = os.getcwd()
        try:
            os.chdir(str(self.home))
            resolved = _resolve_cursor_path(manager_home="sub-home")
        finally:
            os.chdir(original)
        self.assertTrue(resolved.is_absolute())
        self.assertEqual((self.home / "sub-home" / "runtime" / "phase1-cursor.json").resolve(),
                         resolved)

    def test_a_relative_home_that_lands_in_a_checkout_is_still_rejected(self):
        """Resolving relative paths must not smuggle one past the worktree guard."""
        original = os.getcwd()
        checkout = self.home / "co"
        (checkout / ".git").mkdir(parents=True)
        try:
            os.chdir(str(self.home))
            with self.assertRaises(ManagerHomeError):
                _resolve_cursor_path(manager_home="co")
        finally:
            os.chdir(original)


if __name__ == "__main__":
    unittest.main()
