"""File-backed Store/Registry doubles for literal OS-process-boundary
recovery proofs (Gap 2 of the Strengthened Design A activation-readiness
closure).

Not used in production. A genuinely separate `python -m` subprocess
invocation loads its state from these plain JSON files on disk -- there is
no Python object, class instance, in-memory closure, or worker state a
fresh interpreter could inherit from a prior one; the ONLY channel between
two invocations is what got durably written to disk. This is what makes
these doubles suitable for a true fresh-OS-process proof, unlike
MemoryStore/MemoryClaimRegistry (real objects that only ever prove recovery
across fresh Python OBJECTS within the same process).

No real GCS/Drive credentials are available in this sandboxed environment,
so this is SUBPROCESS_DURABLE_PROOF (a genuine OS-process boundary over a
durable file-backed double), not REAL_GCS_DRIVE_PROOF (an actual GCS
object + Drive API round trip) -- the two are deliberately not conflated.
"""

import json
import os
from copy import deepcopy

from manager.gcs_lock_registry import RegistryConflict
from manager.tasks import TaskError


class FileStore:
    """Drop-in Store double (get/put/list_records), backed by one JSON
    file instead of a Python dict living in process memory."""

    def __init__(self, path):
        self.path = path
        if not os.path.exists(path):
            self._save({})

    def _load(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def _save(self, data):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    @staticmethod
    def _key(area, project_id, name):
        return f"{area}::{project_id}::{name}"

    def get(self, area, project_id, name):
        data = self._load()
        key = self._key(area, project_id, name)
        if key not in data:
            raise TaskError(f"not found: {key}")
        return deepcopy(data[key])

    def put(self, area, project_id, name, document):
        data = self._load()
        data[self._key(area, project_id, name)] = deepcopy(document)
        self._save(data)
        return document

    def list_records(self, area, project_id):
        data = self._load()
        prefix = f"{area}::{project_id}::"
        return [deepcopy(value) for key, value in data.items() if key.startswith(prefix)]


class FileClaimRegistry:
    """Drop-in registry double implementing the exact CAS contract
    task_root.py/task_claims.py already program against
    (read_if_exists/read/create_if_absent/compare_and_swap/
    delete_if_generation_matches), backed by one JSON file holding
    {"document":..., "generation":...} instead of instance attributes."""

    def __init__(self, path):
        self.path = path
        if not os.path.exists(path):
            self._save({"document": None, "generation": 0})

    def _load(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def _save(self, state):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, self.path)

    def read_if_exists(self):
        state = self._load()
        if state["document"] is None:
            return None
        return deepcopy(state["document"]), state["generation"], None

    def read(self):
        result = self.read_if_exists()
        if result is None:
            raise TaskError("simulated GCS 404")
        return result

    def create_if_absent(self, document):
        state = self._load()
        if state["document"] is not None:
            raise RegistryConflict("GCS generation precondition failed")
        state["generation"] += 1
        state["document"] = deepcopy(document)
        self._save(state)
        return state["generation"]

    def compare_and_swap(self, expected_generation, document):
        state = self._load()
        if state["document"] is None or state["generation"] != expected_generation:
            raise RegistryConflict("GCS generation precondition failed")
        state["generation"] += 1
        state["document"] = deepcopy(document)
        self._save(state)
        return state["generation"]

    def delete_if_generation_matches(self, expected_generation):
        state = self._load()
        if state["document"] is None or state["generation"] != expected_generation:
            raise RegistryConflict("GCS generation precondition failed on delete")
        state["document"] = None
        self._save(state)
        return True

    @property
    def document(self):
        return self._load()["document"]

    @property
    def generation(self):
        return self._load()["generation"]
