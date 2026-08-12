import json
import threading
import unittest

from manager.gcs_lock_registry import GCSLockRegistry, RegistryConflict
from manager.tasks import TaskError


DATE = "Thu, 13 Aug 2026 00:00:00 GMT"


class Response:
    def __init__(self, status, document=None):
        self.status_code = status
        self._document = document
        self.headers = {"Date": DATE}

    def json(self):
        return self._document


class GenerationStore:
    def __init__(self):
        self.generation = None
        self.document = None
        self.lock = threading.Lock()
        self.unavailable = False

    def get(self, _url, params=None, timeout=None):
        if self.unavailable:
            raise OSError("unavailable")
        with self.lock:
            if self.generation is None:
                return Response(404, {})
            if params and "ifGenerationMatch" in params and int(params["ifGenerationMatch"]) != self.generation:
                return Response(412, {})
            value = self.document if params and params.get("alt") == "media" else {"generation": str(self.generation)}
            return Response(200, json.loads(json.dumps(value)))

    def post(self, _url, params=None, headers=None, data=None, timeout=None):
        expected = int(params["ifGenerationMatch"])
        with self.lock:
            if (expected == 0 and self.generation is not None) or (expected != 0 and expected != self.generation):
                return Response(412, {})
            self.generation = (self.generation or 0) + 1
            self.document = json.loads(data.decode("utf-8"))
            return Response(200 if expected else 201, {"generation": str(self.generation)})


class GCSLockRegistryTests(unittest.TestCase):
    def backend(self, store=None):
        return GCSLockRegistry("test-bucket", "locks/global.json", store or GenerationStore())

    def test_create_if_absent_has_exactly_one_winner(self):
        store = GenerationStore(); first = self.backend(store); second = self.backend(store)
        barrier = threading.Barrier(2); winners, losers = [], []
        def create(backend):
            barrier.wait()
            try: winners.append(backend.create_if_absent({"schema_version": "0.2.0", "locks": {}}))
            except RegistryConflict: losers.append(True)
        threads = [threading.Thread(target=create, args=(backend,)) for backend in (first, second)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual([1], winners); self.assertEqual([True], losers)

    def test_generation_update_and_stale_update(self):
        backend = self.backend(); backend.create_if_absent({"value": 1})
        document, generation, server_time = backend.read()
        self.assertEqual({"value": 1}, document); self.assertEqual(1, generation); self.assertIsNotNone(server_time.tzinfo)
        self.assertEqual(2, backend.compare_and_swap(generation, {"value": 2}))
        with self.assertRaises(RegistryConflict): backend.compare_and_swap(generation, {"value": 3})
        self.assertEqual({"value": 2}, backend.read()[0])

    def test_concurrent_same_generation_exactly_one_succeeds(self):
        store = GenerationStore(); backend = self.backend(store); backend.create_if_absent({"winner": None})
        generation = backend.read()[1]
        barrier = threading.Barrier(2); winners, losers = [], []

        def write(label):
            barrier.wait()
            try: backend.compare_and_swap(generation, {"winner": label}); winners.append(label)
            except RegistryConflict: losers.append(label)

        threads = [threading.Thread(target=write, args=(label,)) for label in ("A", "B")]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(1, len(winners)); self.assertEqual(1, len(losers))
        self.assertEqual(winners[0], backend.read()[0]["winner"])

    def test_unavailable_and_corrupt_registry_fail_closed(self):
        store = GenerationStore(); backend = self.backend(store); store.unavailable = True
        with self.assertRaisesRegex(TaskError, "read failed"): backend.read()
        store.unavailable = False; backend.create_if_absent(["not", "an", "object"])
        with self.assertRaises(TaskError): backend.read()


if __name__ == "__main__": unittest.main()
