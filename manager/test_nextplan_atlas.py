import copy
import itertools
import json
import unittest
from pathlib import Path

from manager.nextplan import vocabulary as v
from manager.nextplan.atlas import (
    ATLAS_PATH, REQUIRED_CODES, Atlas, AtlasError, default_atlas, load_atlas, validate_atlas,
)

FIXTURES = Path(__file__).parent / "nextplan" / "fixtures" / "atlas"
SPEC_FIELDS = (
    "failure_code", "category", "severity", "detection_signal", "required_evidence", "safe_default",
    "retryable", "max_retry_policy", "reroute_allowed", "human_gate_required", "terminal_if_unresolved",
    "next_state", "notes",
)


def raw_atlas():
    return json.loads(ATLAS_PATH.read_text(encoding="utf-8"))


def mutated(code, **changes):
    document = raw_atlas()
    for entry in document["entries"]:
        if entry["failure_code"] == code:
            entry.update(changes)
    return document


class CanonicalAtlasTests(unittest.TestCase):
    def test_canonical_atlas_validates(self):
        validate_atlas(raw_atlas())

    def test_every_required_failure_code_is_defined(self):
        self.assertEqual(set(), REQUIRED_CODES - set(default_atlas().codes))
        self.assertEqual(48, len(REQUIRED_CODES))

    def test_every_entry_carries_every_spec_field(self):
        for entry in raw_atlas()["entries"]:
            self.assertEqual([], [field for field in SPEC_FIELDS if field not in entry], entry["failure_code"])

    def test_every_category_has_entries(self):
        counts = {category: 0 for category in v.CATEGORIES}
        for entry in raw_atlas()["entries"]:
            counts[entry["category"]] += 1
        self.assertTrue(all(count >= 7 for count in counts.values()), counts)

    def test_no_failure_can_route_to_completion(self):
        atlas = default_atlas()
        for code in atlas.codes:
            entry = atlas.entry(code)
            reachable = {entry["safe_default"], entry["fallback_action"], entry["terminal_if_unresolved"],
                         *entry["role_overrides"].values()}
            self.assertNotIn(v.MARK_COMPLETE, reachable, code)

    def test_digest_is_content_addressed(self):
        self.assertEqual(load_atlas().digest, load_atlas().digest)
        document = raw_atlas()
        document["entries"][0]["notes"] += " "
        self.assertNotEqual(default_atlas().digest, Atlas(document).digest)

    def test_entries_cannot_be_mutated_by_callers(self):
        entry = default_atlas().entry("push_failed")
        with self.assertRaises(TypeError):
            entry["safe_default"] = v.MARK_COMPLETE
        with self.assertRaises(TypeError):
            entry["max_retry_policy"]["max_attempts"] = 99

    def test_unknown_code_is_an_error_not_a_default(self):
        with self.assertRaises(AtlasError):
            default_atlas().entry("not_a_failure")

    def test_governing_failure_is_most_severe_then_declared_first(self):
        atlas = default_atlas()
        self.assertEqual("false_pass", atlas.governing({"test_failed", "false_pass", "push_failed"}))
        self.assertEqual("wrong_repo", atlas.governing({"scope_violation", "wrong_repo"}))
        self.assertIsNone(atlas.governing(set()))

    def test_governing_failure_is_independent_of_input_order(self):
        atlas = default_atlas()
        codes = ["push_failed", "test_failed", "rate_limited", "reviewer_fail"]
        self.assertEqual(1, len({atlas.governing(p) for p in itertools.permutations(codes)}))

    def test_role_override_only_changes_the_named_role(self):
        atlas = default_atlas()
        self.assertEqual(v.SEND_TO_REVIEW, atlas.action_for("malformed_result", v.REVIEWER))
        self.assertEqual(v.CONTINUE_WORKER, atlas.action_for("malformed_result", v.WORKER))


class SemanticRuleTests(unittest.TestCase):
    def assertRejected(self, document, fragment):
        with self.assertRaises(AtlasError) as caught:
            validate_atlas(document)
        self.assertTrue(any(fragment in problem for problem in caught.exception.problems), caught.exception.problems)

    def test_next_state_must_follow_from_the_action(self):
        self.assertRejected(mutated("push_failed", next_state=v.COMPLETE), "does not follow from safe_default")

    def test_round_consuming_action_needs_a_finite_budget(self):
        self.assertRejected(mutated("agent_crash", retryable=False), "must be retryable")
        self.assertRejected(
            mutated("agent_crash", max_retry_policy={"max_attempts": 0, "backoff_seconds": 0, "budget": "per_code"}),
            "must be retryable")

    def test_hold_action_cannot_claim_retries(self):
        self.assertRejected(mutated("wrong_repo", retryable=True), "retryable must be false")

    def test_budget_is_bounded_by_schema(self):
        self.assertRejected(
            mutated("agent_crash", max_retry_policy={"max_attempts": 1000, "backoff_seconds": 0, "budget": "per_code"}),
            "schema:")

    def test_failure_cannot_complete_through_any_route(self):
        self.assertRejected(mutated("test_failed", fallback_action=v.MARK_COMPLETE), "MARK_COMPLETE")

    def test_human_gate_cannot_be_bypassed(self):
        self.assertRejected(mutated("ssot_conflict", safe_default=v.RETRY_SAME_AGENT, next_state=v.UNCHANGED),
                            "human_gate_required but safe_default")
        self.assertRejected(mutated("ssot_conflict", fallback_action=v.WAIT_DEPENDENCY), "human-gated failure")

    def test_reroute_must_be_allowed(self):
        self.assertRejected(mutated("reviewer_fail", fallback_action=v.REROUTE_AGENT), "reroute_allowed is false")

    def test_unresolved_behaviour_is_always_a_hold(self):
        self.assertRejected(mutated("push_failed", terminal_if_unresolved=v.NO_OP), "is not a hold action")

    def test_no_op_failures_stay_pure(self):
        self.assertRejected(mutated("duplicate_result", fallback_action=v.HUMAN_GATE), "NO_OP failure")

    def test_repair_budget_only_pays_for_repair(self):
        self.assertRejected(
            mutated("test_failed", max_retry_policy={"max_attempts": 2, "backoff_seconds": 0, "budget": "repair"}),
            "budget 'repair'")

    def test_duplicate_code_rejected(self):
        document = raw_atlas()
        document["entries"].append(copy.deepcopy(document["entries"][0]))
        self.assertRejected(document, "duplicate failure_code")

    def test_missing_required_code_rejected(self):
        document = raw_atlas()
        document["entries"] = [e for e in document["entries"] if e["failure_code"] != "false_pass"]
        self.assertRejected(document, "required failure_code missing: false_pass")


class InvalidAtlasFixtureTests(unittest.TestCase):
    def test_every_invalid_fixture_is_rejected_for_its_stated_reason(self):
        fixtures = sorted(FIXTURES.glob("invalid_*.json"))
        self.assertGreaterEqual(len(fixtures), 6)
        for path in fixtures:
            case = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(path.name):
                with self.assertRaises(AtlasError) as caught:
                    validate_atlas(case["atlas"], required_codes=())
                self.assertTrue(any(case["expect_problem"] in p for p in caught.exception.problems),
                                caught.exception.problems)


if __name__ == "__main__":
    unittest.main()
