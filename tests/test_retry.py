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
    "filament_retry_test", str(Path(__file__).resolve().parents[1] / "skill/bin/filament")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
filament = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, dynamic_credentials=dc):
    original_path = sys.path[:]
    try:
        loader.exec_module(filament)
    finally:
        sys.path[:] = original_path


def failing(*errors):
    calls = []

    def fn():
        calls.append(None)
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1]
        return "ok"
    return fn, calls


class CallWithRetryTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(filament.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_deterministic_rpc_errors_are_not_retried(self):
        for code in (-32600, -32601, -32602):
            with self.subTest(code=code):
                self.sleep.reset_mock()
                error = filament.FilamentError(f"JSON-RPC error {code}: bad", code=code, rejected=True)
                fn, calls = failing(error)
                with self.assertRaises(filament.FilamentError) as raised:
                    filament.call_with_retry(fn, None, "set_profile")
                self.assertIs(raised.exception, error)
                self.assertEqual(len(calls), 1)
                self.sleep.assert_not_called()

    def test_other_rpc_errors_are_still_retried(self):
        error = filament.FilamentError("JSON-RPC error -32603: boom", code=-32603, rejected=True)
        fn, calls = failing(error, error)
        self.assertEqual(filament.call_with_retry(fn, None, "poll_work"), "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [2, 4])

    def test_auth_errors_still_propagate_at_once(self):
        error = filament.FilamentError("auth error -32001: no", auth=True, code=-32001, rejected=True)
        fn, calls = failing(error)
        with self.assertRaises(filament.FilamentError):
            filament.call_with_retry(fn, None, "get_self")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
