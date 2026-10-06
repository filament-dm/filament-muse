import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


dc = types.ModuleType("dynamic_credentials")
dc.add_surrogate_to_request = lambda *args, **kwargs: None
dc.read_response_body = lambda response: response.read()
dc.DynamicCredentialError = type("DynamicCredentialError", (Exception,), {})
loader = SourceFileLoader(
    "filament_ack_test", str(Path(__file__).resolve().parents[1] / "skill/bin/filament")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
filament = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, dynamic_credentials=dc):
    original_path = sys.path[:]
    try:
        loader.exec_module(filament)
    finally:
        sys.path[:] = original_path


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def tool_call(self, name, arguments, timeout=30):
        self.calls.append((name, arguments))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class AckOnlyTests(unittest.TestCase):
    def test_acks_without_asking_for_work(self):
        client = FakeClient({"content": []})

        filament._ack_only(client, ["$a"])

        self.assertEqual(
            client.calls,
            [("poll_work", {"wait_seconds": 0, "ack": ["$a"], "max_items": 0})],
        )

    def test_falls_back_when_the_server_rejects_zero(self):
        client = FakeClient(
            filament.FilamentError("JSON-RPC error -32602: max_items"),
            {"content": []},
        )

        filament._ack_only(client, ["$a"])

        self.assertEqual(
            client.calls[1], ("poll_work", {"wait_seconds": 0, "ack": ["$a"]})
        )

    def test_other_errors_propagate(self):
        client = FakeClient(filament.FilamentError("JSON-RPC error -32603: boom"))

        with self.assertRaises(filament.FilamentError):
            filament._ack_only(client, ["$a"])


if __name__ == "__main__":
    unittest.main()
