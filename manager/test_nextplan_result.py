import json
import unittest

from manager.nextplan import result as r
from manager.nextplan import vocabulary as v

SHA = "a" * 40


def minimal_report(**changes):
    report = {"schema_version": r.REPORT_SCHEMA_VERSION, "task_id": "t-1", "status": "PASS"}
    report.update(changes)
    return report


class SchemaDriftTests(unittest.TestCase):
    def test_committed_schemas_match_the_field_table(self):
        self.assertEqual(r.report_schema(), json.loads(r.REPORT_SCHEMA_PATH.read_text(encoding="utf-8")))
        self.assertEqual(r.normalized_schema(), json.loads(r.RESULT_SCHEMA_PATH.read_text(encoding="utf-8")))

    def test_contract_covers_every_required_field(self):
        required = {
            "task_id", "session_id", "agent", "model", "mode", "status", "progress_percent", "repo", "worktree",
            "branch", "base_sha", "head_sha", "files_changed", "files_created", "files_deleted", "tests_run",
            "tests_passed", "tests_failed", "tests_skipped", "commit_sha", "push_status", "remote_sha",
            "git_status", "review_required", "review_status", "recommended_next_action", "terminal_state",
        }
        self.assertEqual(set(), required - set(r.FACT_FIELDS))
        self.assertIn("ssot_sync_status", r.report_schema()["properties"])
        for key in ("evidence", "blockers", "warnings"):
            self.assertIn(key, r.normalized_schema()["required"])


class NormalizedResultTests(unittest.TestCase):
    def test_blank_result_is_valid_and_entirely_unknown(self):
        blank = r.blank_result("e-1", "t-1", v.WORKER)
        r.validate_result(blank)
        self.assertEqual({v.UNKNOWN}, {r.level(blank, field) for field in r.FACT_FIELDS})

    def test_every_legal_level_source_binding_is_accepted(self):
        legal = [
            r.fact("PASS", v.REPORTED, "agent:native"),
            r.fact("PASS", v.REPORTED, "agent:fenced"),
            r.fact("PASS", v.REPORTED, "agent:deterministic"),
            r.fact("PASS", v.DERIVED, "agent:heuristic"),
            r.fact("FAIL", v.DERIVED, "derived:counts_imply_failure"),
            r.fact("PASS", v.VERIFIED, "probe:test_evidence"),
            r.fact("PASS", v.CONTRADICTED, "probe:test_evidence", "observed FAIL"),
            r.fact(None, v.UNKNOWN, "probe:git", "probe could not run"),
            r.unknown(),
        ]
        for item in legal:
            with self.subTest(item):
                r.validate_result(r.with_fact(r.blank_result("e", "t", v.WORKER), "status", item))

    def test_no_stage_can_promote_a_claim_without_a_probe(self):
        illegal = {
            "agent says VERIFIED": r.fact("PASS", v.VERIFIED, "agent:fenced"),
            "heuristic says VERIFIED": r.fact("PASS", v.VERIFIED, "agent:heuristic"),
            "heuristic says REPORTED": r.fact("PASS", v.REPORTED, "agent:heuristic"),
            "derived says VERIFIED": r.fact("PASS", v.VERIFIED, "derived:anything"),
            "agent CONTRADICTED": r.fact("PASS", v.CONTRADICTED, "agent:native"),
            "structured says DERIVED": r.fact("PASS", v.DERIVED, "agent:fenced"),
            "UNKNOWN with a value": r.fact("PASS", v.UNKNOWN, "absent"),
            "REPORTED without a value": r.fact(None, v.REPORTED, "agent:fenced"),
            "UNKNOWN from agent": r.fact(None, v.UNKNOWN, "agent:fenced"),
        }
        for name, item in illegal.items():
            with self.subTest(name):
                with self.assertRaises(r.ResultError):
                    r.with_fact(r.blank_result("e", "t", v.WORKER), "status", item)

    def test_schema_rejects_wrong_value_types(self):
        blank = r.blank_result("e", "t", v.WORKER)
        for field, bad in (("tests_run", -1), ("progress_percent", 101), ("head_sha", "not-a-sha"),
                           ("status", "LOOKS_GOOD"), ("files_changed", "one.py")):
            with self.subTest(field):
                blank["facts"][field] = r.fact(bad, v.REPORTED, "agent:fenced")
                self.assertTrue(r.result_problems(blank))
                blank["facts"][field] = r.unknown()

    def test_with_fact_never_mutates_its_input(self):
        blank = r.blank_result("e", "t", v.WORKER)
        updated = r.with_fact(blank, "head_sha", r.fact(SHA, v.VERIFIED, "probe:git"))
        self.assertEqual(v.UNKNOWN, r.level(blank, "head_sha"))
        self.assertTrue(r.is_verified(updated, "head_sha"))

    def test_unknown_fact_field_is_rejected(self):
        with self.assertRaises(r.ResultError):
            r.with_fact(r.blank_result("e", "t", v.WORKER), "looks_done", r.unknown())

    def test_recommended_next_action_is_advisory(self):
        self.assertIn("recommended_next_action", r.ADVISORY_FIELDS)


class ReportSchemaTests(unittest.TestCase):
    def test_minimal_report_is_valid(self):
        self.assertEqual([], r.report_problems(minimal_report()))

    def test_full_report_is_valid(self):
        report = minimal_report(
            session_id="s-1", agent="claude", model="claude-opus-5", mode="high", progress_percent=100,
            repo="github:ne9221/ai-development-manager", worktree="/w", branch="feat/x", base_sha=SHA, head_sha=SHA,
            files_changed=["a.py"], files_created=[], files_deleted=[], tests_run=3, tests_passed=3,
            tests_failed=0, tests_skipped=0, commit_sha=SHA, commits=[SHA], push_status="pushed", remote_sha=SHA,
            git_status="clean", dirty_paths=[], ssot_sync_status={"github": "synced", "drive": None},
            evidence=[{"kind": "test_log", "ref": "pytest.txt"}], blockers=[], warnings=[],
            review_required=True, review_status="pending", recommended_next_action="review", terminal_state="terminal",
        )
        self.assertEqual([], r.report_problems(report))

    def test_report_requires_identity_and_status(self):
        report = minimal_report()
        del report["status"]
        self.assertTrue(r.report_problems(report))
        self.assertTrue(r.report_problems(minimal_report(schema_version="adm-ai-result/0")))

    def test_report_rejects_malformed_values(self):
        self.assertTrue(r.report_problems(minimal_report(commit_sha="HEAD")))
        self.assertTrue(r.report_problems(minimal_report(status="done")))
        self.assertTrue(r.report_problems(minimal_report(tests_failed="0")))

    def test_report_tolerates_extra_keys(self):
        self.assertEqual([], r.report_problems(minimal_report(provider_note="extra")))


if __name__ == "__main__":
    unittest.main()
