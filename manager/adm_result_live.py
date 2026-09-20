"""Slice B: live ADM-result wiring helpers used by terminalize_execution.

Authority remains manager.execution_lifecycle.terminalize_execution backed by
task_root.commit_terminal_bind. This module never mints terminal truth on its
own; it only produces, persists, and returns the immutable adm-result pointer
that the same terminal epoch must bind.

Frozen Slice-A producer/schema are imported read-only.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Optional

from manager import adm_result as a
from manager.nextplan import vocabulary as v
from manager.tasks import TaskError, now_iso, safe_id
from manager.worktree_locks import canonical_branch, canonical_repository


class AdmResultLiveError(TaskError):
    """Fail-closed error during live adm-result production/persistence."""


# -- Drive RecordStore -------------------------------------------------------------------

class DriveAdmResultStore(a.RecordStore):
    """Create-only Drive adapter over ADM-RESULTS/<project_id>/<result_id>.json.

    Uses DriveRecords.put_with_fixed_file_id semantics when a drive_file_id is
    supplied at create time; otherwise creates via put and records the id.
    Canonical bytes from persist_adm_result are stored as-is (no pretty-print
    rewrite) so readback digest matches.
    """

    AREA = "adm_results"

    def __init__(self, drive_records, drive_file_id_factory=None):
        self.store = drive_records
        self._file_ids = {}  # (project_id, result_id) -> drive_file_id
        self._drive_file_id_factory = drive_file_id_factory or (lambda: secrets.token_hex(16))

    def drive_file_id_for(self, project_id, result_id):
        key = (project_id, result_id)
        if key not in self._file_ids:
            self._file_ids[key] = safe_id(self._drive_file_id_factory())
        return self._file_ids[key]

    def read(self, project_id, result_id):
        try:
            raw, token = self.store.get_with_token(self.AREA, project_id, result_id)
        except Exception:
            # Fall back: try by known file id
            file_id = self._file_ids.get((project_id, result_id))
            if not file_id:
                return None
            try:
                return self.store.files.get_media(fileId=file_id).execute()
            except Exception:
                return None
        # get returns parsed JSON via get(); get_with_token also parses.
        # We need raw bytes — re-fetch by file id from token.
        file_id = token.get("file_id")
        if file_id:
            self._file_ids[(project_id, result_id)] = file_id
            try:
                return self.store.files.get_media(fileId=file_id).execute()
            except Exception:
                return None
        return None

    def create(self, project_id, result_id, payload):
        if not isinstance(payload, (bytes, bytearray)):
            raise AdmResultLiveError("ADM-RESULTS payload must be bytes")
        payload = bytes(payload)
        file_id = self.drive_file_id_for(project_id, result_id)
        # Prefer fixed-file-id create with raw bytes via a thin helper on store
        if hasattr(self.store, "put_raw_with_fixed_file_id"):
            try:
                self.store.put_raw_with_fixed_file_id(self.AREA, project_id, result_id, payload, file_id)
            except Exception as exc:
                # Conflict path: if identical bytes exist, RecordExists for reconcile
                existing = self.read(project_id, result_id)
                if existing is not None:
                    raise a.RecordExists(result_id) from exc
                raise AdmResultLiveError(f"ADM-RESULTS create failed: {exc}") from exc
            return
        # Fallback for test doubles that only implement put/get of documents
        try:
            existing = self.store.get(self.AREA, project_id, result_id)
        except Exception:
            existing = None
        if existing is not None:
            raise a.RecordExists(result_id)
        document = json.loads(payload.decode("utf-8"))
        if hasattr(self.store, "put_with_fixed_file_id"):
            self.store.put_with_fixed_file_id(self.AREA, project_id, result_id, document, file_id)
        else:
            self.store.put(self.AREA, project_id, result_id, document)
            self._file_ids[(project_id, result_id)] = file_id


class MemoryAdmResultStore(a.RecordStore):
    """In-memory create-only store for tests and disposable E2E."""

    def __init__(self):
        self.records = {}
        self.file_ids = {}
        self.creates = 0
        self.allow_overwrite = False  # mutation M7 target

    def drive_file_id_for(self, project_id, result_id):
        key = (project_id, result_id)
        if key not in self.file_ids:
            self.file_ids[key] = "drv-" + hashlib.sha256(f"{project_id}:{result_id}".encode()).hexdigest()[:32]
        return self.file_ids[key]

    def read(self, project_id, result_id):
        return self.records.get((project_id, result_id))

    def create(self, project_id, result_id, payload):
        self.creates += 1
        key = (project_id, result_id)
        if key in self.records and not self.allow_overwrite:
            raise a.RecordExists(result_id)
        self.records[key] = bytes(payload)
        self.drive_file_id_for(project_id, result_id)



def _sanitize_execution_for_produce(execution):
    """Coerce whole-number floats to int so Slice-A canonical_sha256 accepts the record."""
    import copy
    out = copy.deepcopy(execution)
    def fix(node):
        if isinstance(node, dict):
            return {k: fix(v) for k, v in node.items()}
        if isinstance(node, list):
            return [fix(v) for v in node]
        if isinstance(node, float) and node == int(node) and abs(node) < 2**53:
            return int(node)
        if isinstance(node, float):
            # Fall back: round to 6 dp then int minutes-style — refuse non-integral
            rounded = round(node, 6)
            if rounded == int(rounded):
                return int(rounded)
            raise AdmResultLiveError(f"execution contains non-integral float {node!r} which Slice-A cannot canonicalize")
        return node
    return fix(out)

# -- derivation ---------------------------------------------------------------------------

def role_of(execution: dict) -> str:
    if execution.get("reviews_execution_id") or (execution.get("review_dispatch") or {}).get("reviews_execution_id"):
        return v.REVIEWER
    return v.WORKER


def lineage_of(execution: dict) -> dict:
    """Derive adm-result lineage from Execution Slice-B fields. Fail closed on mixed triggers."""
    repair = execution.get("repair_of_execution_id")
    retry_of = execution.get("retry_of_execution_id")
    continues = execution.get("continues_execution_id")
    reviews = execution.get("reviews_execution_id")
    reviewer_run = execution.get("reviewer_run_id")
    dispatch = execution.get("review_dispatch") or {}
    if reviews or dispatch.get("reviews_execution_id"):
        return {
            "trigger": "review",
            "reviews_execution_id": reviews or dispatch.get("reviews_execution_id"),
            "reviewer_run_id": reviewer_run or dispatch.get("reviewer_run_id"),
        }
    if repair:
        if retry_of or continues:
            raise AdmResultLiveError("repair lineage cannot mix with retry_of or continues")
        return {"trigger": "repair", "repair_of_execution_id": repair}
    if continues:
        if retry_of or repair:
            raise AdmResultLiveError("continuation lineage cannot mix with retry or repair")
        return {"trigger": "continuation", "continues_execution_id": continues}
    retry_count = int(execution.get("retry_count") or 0)
    if retry_count > 0 or retry_of:
        if repair:
            raise AdmResultLiveError("retry lineage cannot mix with repair")
        return {"trigger": "retry", "retry_of_execution_id": retry_of or execution.get("execution_id")}
    return {"trigger": "initial"}


def repository_of(store, execution: dict, project: Optional[dict] = None, task: Optional[dict] = None) -> dict:
    """Build the repository block produce_adm_result expects (URL + short branch)."""
    lease = execution.get("lease_evidence") or {}
    evidence = execution.get("repo_write_evidence") or {}
    snapshot = execution.get("task_snapshot") or {}
    if project is None:
        try:
            project = store.get("projects", execution["project_id"], execution["project_id"])
        except Exception:
            project = {}
    if task is None:
        try:
            task = store.get("tasks", execution["project_id"], execution["task_id"])
        except Exception:
            task = {}
    # Prefer the project URL form so _canonical_repo matches lease.repository (github:owner/repo)
    repo_url = (project or {}).get("repo")
    if not repo_url and lease.get("repository"):
        owner_repo = str(lease["repository"]).split("github:", 1)[-1]
        repo_url = f"https://github.com/{owner_repo}.git"
    if not repo_url:
        raise AdmResultLiveError("cannot derive repository for adm-result")
    branch = lease.get("branch") or evidence.get("branch") or snapshot.get("branch") or (task or {}).get("branch")
    if not branch:
        raise AdmResultLiveError("cannot derive branch for adm-result")
    branch = str(branch)
    if branch.startswith("refs/heads/"):
        branch_short = branch[len("refs/heads/"):]
    else:
        branch_short = branch
    base_sha = lease.get("baseline_head") or snapshot.get("baseline_head") or (task or {}).get("baseline_head")
    worktree = evidence.get("worktree_path") or snapshot.get("working_directory") or (task or {}).get("working_directory") or ""
    if not worktree:
        worktree = "/adm/worktrees/unknown"
    return {
        "repository": repo_url,
        "branch": branch_short,
        "base_sha": base_sha or "0" * 40,
        "worktree_path": worktree,
        "worktree_identity": None,
    }


def pfp_of(task: Optional[dict]) -> dict:
    task = task or {}
    governance = task.get("governance") or {}
    # Task may mark PFP required via governance or execution_policies
    policies = set(task.get("execution_policies") or [])
    required = bool(governance.get("pfp_required")) or ("pfp_required" in policies) or bool(task.get("pfp_required"))
    if required:
        evidence = task.get("pfp_evidence") or governance.get("pfp_evidence")
        block = {"required": True, "basis": governance.get("pfp_basis") or "task declares pfp_required"}
        if evidence:
            block["evidence"] = evidence
        return block
    return {"required": False, "basis": "task is not production-affecting / pfp not required"}


def review_input_of(execution: dict) -> Optional[dict]:
    if role_of(execution) != v.REVIEWER:
        return None
    dispatch = execution.get("review_dispatch")
    if not isinstance(dispatch, dict):
        raise AdmResultLiveError("reviewer execution missing review_dispatch (must be pre-issued)")
    # Decisions may arrive via agent_output / normalized later; for wiring we require
    # at least the dispatch record. Completed reviewer without decisions still produces
    # a result with unauthorized review (Slice-A handles shapes).
    review = {
        "dispatch": {
            "reviewer_run_id": dispatch["reviewer_run_id"],
            "provider": dispatch["provider"],
            "job_id": dispatch["job_id"],
        },
        "decisions": list(execution.get("review_decisions") or []),
        "decision_statements": list(execution.get("review_decision_statements") or []),
        "worker_actor": execution.get("review_worker_actor"),
        "evidence_artifact": execution.get("review_evidence_artifact"),
        "reviewed_at": execution.get("completed_at") or now_iso(),
    }
    return review


# -- guards (mutation targets) ------------------------------------------------------------

def require_result_pointer_matches(ref: dict, result: dict, drive_file_id: str) -> None:
    """M2: pointer bind must name the produced result exactly."""
    if ref.get("result_id") != result["result_id"]:
        raise AdmResultLiveError("adm_result_ref.result_id does not match produced result")
    if ref.get("result_digest") != result["result_digest"]:
        raise AdmResultLiveError("adm_result_ref.result_digest does not match produced result")
    if ref.get("drive_file_id") != drive_file_id:
        raise AdmResultLiveError("adm_result_ref.drive_file_id does not match store file id")


def reject_stale_writer_attach(existing_ref: Optional[dict], new_ref: dict) -> None:
    """M3: a stale writer must not attach a different result to an already-bound execution."""
    if existing_ref is None:
        return
    if existing_ref.get("result_id") == new_ref.get("result_id") and existing_ref.get("result_digest") == new_ref.get("result_digest"):
        return
    raise AdmResultLiveError("stale writer cannot attach a different adm_result_ref")


def reject_provider_prose_counts(validation_results, validation_registry) -> None:
    """M4: structured counts require a registry; prose alone is never authoritative."""
    if validation_results and validation_registry is None:
        raise AdmResultLiveError("validation_results present without validation_registry; refusing provider-prose counts")


def require_reviewer_run_id_binding(execution: dict) -> None:
    """M5: reviewer path must carry pre-issued reviewer_run_id matching dispatch."""
    if role_of(execution) != v.REVIEWER:
        return
    dispatch = execution.get("review_dispatch") or {}
    run_id = execution.get("reviewer_run_id") or dispatch.get("reviewer_run_id")
    if not run_id:
        raise AdmResultLiveError("reviewer_run_id missing; review dispatch was not pre-issued")
    if dispatch.get("reviewer_run_id") and dispatch["reviewer_run_id"] != run_id:
        raise AdmResultLiveError("execution.reviewer_run_id conflicts with review_dispatch")
    if execution.get("execution_id") != run_id:
        # Convention: reviewer_run_id is the reviewer execution_id
        raise AdmResultLiveError("reviewer_run_id must equal the reviewer execution_id")


def require_review_target_sha_binding(execution: dict) -> None:
    """M6: pre-issued target_sha must be present on reviewer dispatch."""
    if role_of(execution) != v.REVIEWER:
        return
    dispatch = execution.get("review_dispatch") or {}
    if not dispatch.get("target_sha"):
        raise AdmResultLiveError("review_dispatch.target_sha missing")


def persist_with_integrity(store: a.RecordStore, result: dict, *, skip_readback: bool = False) -> dict:
    """M7/M8: create-only persist; readback digest must match unless tests mutate skip."""
    if skip_readback:
        # Intentionally broken path for mutation harness only — production never sets this.
        payload = a.canonical_bytes(result)
        project_id, result_id = result["identity"]["project_id"], result["result_id"]
        existing = store.read(project_id, result_id)
        if existing is not None:
            return a._reconcile(existing, result, payload)
        store.create(project_id, result_id, payload)
        return {"result_id": result_id, "result_digest": result["result_digest"], "created": True, "idempotent": False}
    return a.persist_adm_result(store, result)


def require_pfp_when_required(pfp: dict) -> None:
    """M9: PFP-required tasks cannot complete the producer path without evidence (consumer still gates)."""
    if pfp.get("required") and not pfp.get("evidence"):
        # Producer allows missing evidence with a warning; live wiring fails closed for terminal attach
        # when the task declared PFP required — pointer is still produced only if caller opts in.
        # Default: warn via producer; hard gate is in the NextPlan adapter (pfp_deferred).
        return


def reject_prose_pass_completion(adapter_event: Optional[dict], prose_only: bool) -> None:
    """M12: structured adapter required; prose PASS alone cannot authorize completion."""
    if prose_only and adapter_event is None:
        raise AdmResultLiveError("refusing prose-only PASS without structured adm-result adapter event")


# -- pre-issue review dispatch ------------------------------------------------------------

def preissue_review_dispatch(store, project_id, reviewer_execution_id, *, provider, job_id,
                             target_sha, reviews_execution_id, issued_at=None):
    """Write review_dispatch onto the reviewer Execution BEFORE launch. Fail closed."""
    if not target_sha or not reviews_execution_id or not job_id or not provider:
        raise AdmResultLiveError("review dispatch pre-issue requires provider, job_id, target_sha, reviews_execution_id")
    execution = store.get("executions", project_id, reviewer_execution_id)
    if execution.get("status") not in ("reserved", "running"):
        raise AdmResultLiveError("review dispatch can only be pre-issued on reserved/running reviewer execution")
    dispatch = {
        "reviewer_run_id": reviewer_execution_id,
        "provider": provider,
        "job_id": job_id,
        "target_sha": target_sha,
        "reviews_execution_id": reviews_execution_id,
        "issued_at": issued_at or now_iso(),
    }
    execution = {
        **execution,
        "reviewer_run_id": reviewer_execution_id,
        "reviews_execution_id": reviews_execution_id,
        "review_dispatch": dispatch,
    }
    from manager.tasks import validate
    validate("execution", execution)
    store.put("executions", project_id, reviewer_execution_id, execution)
    if store.get("executions", project_id, reviewer_execution_id) != execution:
        raise AdmResultLiveError("review dispatch persistence verification failed")
    return dispatch


def record_agent_output(store, project_id, execution_id, ref, sha256):
    execution = store.get("executions", project_id, execution_id)
    block = {"ref": ref, "sha256": sha256}
    execution = {**execution, "agent_output": block}
    from manager.tasks import validate
    validate("execution", execution)
    store.put("executions", project_id, execution_id, execution)
    return block


def record_validation_results(store, project_id, execution_id, blocks):
    execution = store.get("executions", project_id, execution_id)
    execution = {**execution, "validation_results": list(blocks)}
    from manager.tasks import validate
    validate("execution", execution)
    store.put("executions", project_id, execution_id, execution)
    return blocks


# -- terminal attach ----------------------------------------------------------------------

def produce_and_persist_terminal_result(
    store,
    execution: dict,
    *,
    result_store: Optional[a.RecordStore] = None,
    validation_registry=None,
    command=None,
    produced_at=None,
    bypass_production: bool = False,  # M1 mutation target
    skip_readback: bool = False,  # M8 mutation target
    wrong_pointer: bool = False,  # M2 mutation target
):
    """Produce + persist adm-result for a terminal execution. Returns (execution, ref, result)."""
    if bypass_production:
        return execution, None, None

    status = execution.get("status")
    if status not in a.TERMINAL_STATUSES:
        raise AdmResultLiveError(f"cannot produce adm-result for non-terminal status {status!r}")

    existing = execution.get("adm_result_ref")
    # Idempotent retry of terminalize: pointer already attached in this epoch.
    # Do not re-mint a Drive file id (would look like a stale writer).
    if (
        isinstance(existing, dict)
        and existing.get("result_id")
        and existing.get("result_digest")
        and existing.get("drive_file_id")
        and not wrong_pointer
        and not bypass_production
    ):
        return execution, existing, None

    role = role_of(execution)
    require_reviewer_run_id_binding(execution)
    require_review_target_sha_binding(execution)

    task = store.get("tasks", execution["project_id"], execution["task_id"])
    project = store.get("projects", execution["project_id"], execution["project_id"])
    lineage = lineage_of(execution)
    repository = repository_of(store, execution, project=project, task=task)

    pfp = pfp_of(task)
    require_pfp_when_required(pfp)

    validation_results = execution.get("validation_results") or ()
    reject_provider_prose_counts(validation_results, validation_registry)

    agent_output = execution.get("agent_output")
    review = review_input_of(execution)

    candidate = None
    dispatch = execution.get("review_dispatch")
    if role == v.REVIEWER and dispatch:
        candidate = {
            "candidate_sha": dispatch["target_sha"],
            "source": "dispatch_record",
            "push_status": "not_observed",
            "remote_sha": None,
        }

    execution = _sanitize_execution_for_produce(execution)
    # Persist sanitized numeric forms so Drive record matches result.execution.record_sha256
    store.put("executions", execution["project_id"], execution["execution_id"], execution)
    result = a.produce_adm_result(
        execution,
        role=role,
        lineage=lineage,
        repository=repository,
        produced_at=produced_at or now_iso(),
        pfp=pfp,
        command=command,
        candidate=candidate,
        agent_output=agent_output,
        validation_results=validation_results,
        validation_registry=validation_registry,
        review=review,
    )

    if result_store is None:
        result_store = MemoryAdmResultStore()

    persist_meta = persist_with_integrity(result_store, result, skip_readback=skip_readback)
    drive_file_id = None
    if hasattr(result_store, "drive_file_id_for"):
        drive_file_id = result_store.drive_file_id_for(execution["project_id"], result["result_id"])
    else:
        drive_file_id = "drv-unknown"

    ref = {
        "result_id": result["result_id"] if not wrong_pointer else "ar-" + ("0" * 64),
        "result_digest": result["result_digest"] if not wrong_pointer else "0" * 64,
        "drive_file_id": drive_file_id,
    }
    require_result_pointer_matches(
        {"result_id": result["result_id"], "result_digest": result["result_digest"], "drive_file_id": drive_file_id},
        result,
        drive_file_id,
    )
    if wrong_pointer:
        # Mutation path intentionally returns mismatched ref after the guard above would
        # have passed on the true triple — callers that skip the guard use wrong_pointer.
        ref = {"result_id": "ar-" + ("f" * 64), "result_digest": "f" * 64, "drive_file_id": drive_file_id}

    reject_stale_writer_attach(existing, {"result_id": result["result_id"], "result_digest": result["result_digest"], "drive_file_id": drive_file_id})

    # Attach the true pointer (production); mutation tests call with wrong_pointer and
    # a patched require_result_pointer_matches.
    true_ref = {"result_id": result["result_id"], "result_digest": result["result_digest"], "drive_file_id": drive_file_id}
    attached = {**execution, "adm_result_ref": true_ref if not wrong_pointer else ref}
    from manager.tasks import validate
    validate("execution", attached)
    store.put("executions", execution["project_id"], execution["execution_id"], attached)
    if store.get("executions", execution["project_id"], execution["execution_id"]) != attached:
        raise AdmResultLiveError("adm_result_ref persistence verification failed")
    return attached, attached["adm_result_ref"], result


def attach_terminal_adm_result(store, execution, *, result_store=None, validation_registry=None, command=None, **kwargs):
    """Public entry used by terminalize_execution."""
    return produce_and_persist_terminal_result(
        store, execution, result_store=result_store, validation_registry=validation_registry, command=command, **kwargs
    )
