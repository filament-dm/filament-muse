import contextlib
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import call, patch

from test_context import FakeClient, work_item


dc = types.ModuleType("dynamic_credentials")
dc.add_surrogate_to_request = lambda *args, **kwargs: None
dc.read_response_body = lambda response: response.read()
dc.DynamicCredentialError = type("DynamicCredentialError", (Exception,), {})
loader = SourceFileLoader(
    "filament_resilience_test", str(Path(__file__).resolve().parents[1] / "skill/bin/filament")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
filament = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, dynamic_credentials=dc):
    original_path = sys.path[:]
    try:
        loader.exec_module(filament)
    finally:
        sys.path[:] = original_path


class ListenerResilienceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.now = 1000.0
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(filament, "STATE_DIR", str(self.state)))
        stack.enter_context(patch.object(filament.time, "time", side_effect=lambda: self.now))
        self.sleep = stack.enter_context(patch.object(filament.time, "sleep", side_effect=self.advance))
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        stack.enter_context(contextlib.redirect_stdout(self.stdout))
        stack.enter_context(contextlib.redirect_stderr(self.stderr))
        stack.enter_context(patch.object(filament.urllib.request, "urlopen",
                                        side_effect=AssertionError("unexpected network request")))
        stack.enter_context(patch.dict(os.environ, FILAMENT_RUN_START="1000"))

    def advance(self, seconds):
        self.now += seconds

    def auth(self, code=None):
        return filament.FilamentError("HTTP 401" if code is None else str(code),
                                      auth=True, code=code)

    def run_cli(self, client, *args):
        with patch.object(filament, "Client", return_value=client):
            return filament.main(list(args))

    def lock(self, role="backstop", legacy=False):
        path = self.state / "run.lock"
        path.write_text("900" if legacy else json.dumps({"start": 900, "role": role}))
        os.utime(path, (self.now, self.now))

    def wanted(self, age=0):
        path = self.state / "frontdoor.wanted"
        path.touch()
        os.utime(path, (self.now - age, self.now - age))

    def handling_lock(self, age=0, role="backstop", pending_ids=None):
        filament.write_state("run.lock", json.dumps({
            "start": 900, "role": role, "handling": self.now - age}))
        os.utime(self.state / "run.lock", (self.now - age, self.now - age))
        filament.write_state("last_delivery.json", json.dumps({
            "cursor": "before-delivery", "ids": pending_ids or ["event"],
            "since": self.now - age}))

    def handling_of(self, role="frontdoor"):
        return {"start": 1000, "role": role, "handling": self.now}

    def test_handling_blocks_backstop_and_ensure_reports_it(self):
        self.handling_lock(age=239)
        client = FakeClient()
        self.assertEqual(self.run_cli(client, "listen", "--budget", "100",
                                      "--role", "backstop"), 3)
        self.assertEqual(client.calls, [])
        self.assertIn("handling", self.stderr.getvalue())
        self.assertEqual(self.run_cli(FakeClient(), "ensure"), 0)
        self.assertEqual(json.loads(self.stdout.getvalue()),
                         {"listener": "handling", "role": "backstop"})
        self.sleep.assert_not_called()

    def test_frontdoor_immediately_claims_its_own_handling_and_saved_cursor(self):
        self.handling_lock(role="frontdoor")
        filament.write_state("cursor", "before-delivery")
        client = FakeClient(polls=[{"work": [work_item()], "cursor": "after"}])
        self.assertEqual(self.run_cli(client, "listen", "--budget", "100",
                                      "--role", "frontdoor"), 0)
        polls = [args for name, args, _ in client.calls if name == "poll_work"]
        self.assertEqual(polls[0]["cursor"], "before-delivery")
        self.assertEqual((self.state / "cursor").read_text(), "before-delivery")
        self.assertEqual(json.loads((self.state / "run.lock").read_text()),
                         self.handling_of())
        self.sleep.assert_not_called()

    def test_stale_handling_is_claimable(self):
        self.handling_lock(age=filament.HANDLING_SECONDS)
        self.assertEqual(self.run_cli(FakeClient(), "ensure"), 0)
        self.assertEqual(json.loads(self.stdout.getvalue()), {"listener": "none"})
        client = FakeClient(polls=[{"work": [work_item()]}])
        self.assertEqual(self.run_cli(client, "listen", "--budget", "100"), 0)
        self.assertEqual(json.loads((self.state / "run.lock").read_text()),
                         self.handling_of())

    def test_delivery_with_nothing_to_answer_releases_the_lock(self):
        client = FakeClient(polls=[{"work": [work_item(reply_with=None)]}])
        self.assertEqual(self.run_cli(client, "listen", "--budget", "100"), 0)
        self.assertFalse((self.state / "run.lock").exists())

    def test_successful_reply_ack_and_reset_clear_handling(self):
        for args in (("ack", "event"),
                     ("reply", "--with", '{"tool":"post_message"}',
                      "--body", "hello", "--for", "reply-event"),
                     ("reply", "--with", '{"tool":"post_message"}',
                      "--body", "hello", "--for", "reply-event"),
                     ("reset",)):
            with self.subTest(args=args):
                self.handling_lock(pending_ids=["reply-event"] if args[0] == "reply" else ["event"])
                self.assertEqual(self.run_cli(FakeClient(polls=[{"acknowledged": 1}]), *args), 0)
                self.assertFalse((self.state / "run.lock").exists())

    def test_frontdoor_waits_for_backstop_handling_expiry(self):
        self.handling_lock()
        client = FakeClient(polls=[{"work": [work_item()]}])
        def wait(seconds):
            self.assertTrue((self.state / "frontdoor.wanted").exists())
            self.assertEqual(client.calls, [])
            self.advance(seconds)
        self.sleep.side_effect = wait
        self.assertEqual(self.run_cli(client, "listen", "--budget", "300"), 0)
        self.assertEqual(self.now, 1240)
        self.assertFalse((self.state / "frontdoor.wanted").exists())
        self.assertEqual(filament._lock_info()[0]["role"], "frontdoor")

    def test_backstop_never_claims_fresh_handling_of_either_role(self):
        for role in ("frontdoor", "backstop"):
            self.handling_lock(role=role)
            client = FakeClient()
            self.assertEqual(self.run_cli(client, "listen", "--budget", "300",
                                          "--role", "backstop"), 3)
            self.assertEqual(client.calls, [])

    def test_reply_refreshes_handling_and_the_last_ack_releases_it(self):
        client = FakeClient(polls=[{"work": [work_item(), work_item(
            messages=[{"event_id": "second", "body": "Another?"}])]}])
        self.assertEqual(self.run_cli(client, "listen", "--budget", "100"), 0)
        self.advance(200)
        self.assertEqual(self.run_cli(FakeClient(), "reply", "--with",
                                      '{"tool":"post_message"}', "--body", "hello",
                                      "--for", "own"), 0)
        self.assertEqual(json.loads((self.state / "last_delivery.json").read_text())["ids"],
                         ["second"])
        content, age = filament._lock_info()
        self.assertEqual(content["start"], "1000")
        self.assertEqual(age, 0)
        self.advance(100)
        self.assertTrue(filament._lock_active(*filament._lock_info()))
        self.assertEqual(self.run_cli(FakeClient(polls=[{"acknowledged": 1}]),
                                      "ack", "second"), 0)
        self.assertFalse((self.state / "run.lock").exists())

    def test_mismatched_reply_ack_and_missing_ids_leave_handling_untouched(self):
        self.handling_lock(pending_ids=["successor"])
        path = self.state / "run.lock"
        before, mtime = path.read_bytes(), path.stat().st_mtime
        self.advance(10)
        for args in (("ack", "old"),
                     ("reply", "--with", '{"tool":"post_message"}',
                      "--body", "hello", "--for", "old"),
                     ("reply", "--with", '{"tool":"post_message"}',
                      "--body", "hello")):
            self.assertEqual(self.run_cli(FakeClient(polls=[{"acknowledged": 1}]), *args), 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mtime, mtime)

    def test_refreshed_backstop_handling_wait_is_bounded(self):
        self.handling_lock()
        def refresh(seconds):
            self.advance(seconds)
            os.utime(self.state / "run.lock", (self.now, self.now))
        self.sleep.side_effect = refresh
        client = FakeClient()
        self.assertEqual(self.run_cli(client, "listen", "--budget", "300"), 3)
        self.assertEqual(self.now, 1240)
        self.assertEqual(client.calls, [])
        self.assertTrue((self.state / "run.lock").exists())
        self.assertFalse((self.state / "frontdoor.wanted").exists())

    def test_lock_and_wanted_mutations_hold_flock(self):
        self.lock()
        def guarded(operation):
            def checked(path, *args, **kwargs):
                if Path(path).name in ("run.lock", "frontdoor.wanted"):
                    with open(self.state / "run.lock.guard", "a") as contender:
                        with self.assertRaises(BlockingIOError):
                            filament.fcntl.flock(contender, filament.fcntl.LOCK_EX |
                                                 filament.fcntl.LOCK_NB)
                return operation(path, *args, **kwargs)
            return checked
        with patch.object(filament, "write_state", guarded(filament.write_state)), \
                patch.object(filament.os, "remove", guarded(filament.os.remove)), \
                patch.object(filament.os, "utime", guarded(filament.os.utime)):
            self.assertEqual(self.run_cli(FakeClient(), "listen", "--budget", "100"), 3)
            self.assertEqual(self.run_cli(FakeClient(), "reset"), 0)
            client = FakeClient(polls=[{"work": [work_item()]}])
            self.assertEqual(self.run_cli(client, "listen", "--budget", "100"), 0)
            self.assertEqual(self.run_cli(FakeClient(polls=[{"acknowledged": 1}]),
                                          "ack", "own"), 0)
            self.assertEqual(self.run_cli(FakeClient(polls=[{"work": []}]),
                                          "listen", "--budget", "1"), 3)

    def test_reply_and_ack_preserve_live_listener(self):
        for args in (("ack", "event"),
                     ("reply", "--with", '{"tool":"post_message"}',
                      "--body", "hello", "--for", "reply-event")):
            with self.subTest(args=args):
                self.lock("frontdoor")
                before = (self.state / "run.lock").read_text()
                self.assertEqual(self.run_cli(FakeClient(polls=[{"acknowledged": 1}]), *args), 0)
                self.assertEqual((self.state / "run.lock").read_text(), before)

    def test_reply_that_never_reached_the_server_preserves_handling(self):
        self.handling_lock(pending_ids=["reply-event"])
        with patch.object(FakeClient, "handshake", side_effect=self.auth()):
            self.assertEqual(self.run_cli(FakeClient(), "reply", "--with",
                                          '{"tool":"post_message"}', "--body", "hello",
                                          "--for", "reply-event"), 2)
        self.assertIn("handling", json.loads((self.state / "run.lock").read_text()))

    def test_bystander_preserves_another_frontdoors_wanted(self):
        self.lock("frontdoor")
        filament.write_state("frontdoor.wanted", "999")
        for role in ("frontdoor", "backstop"):
            self.assertEqual(self.run_cli(FakeClient(), "listen", "--budget", "100",
                                          "--role", role), 3)
            self.assertEqual((self.state / "frontdoor.wanted").read_text(), "999")

    def test_one_401_recovers_same_cursor_and_lock(self):
        client = FakeClient(polls=[{"work": [], "cursor": "saved", "truncated": True},
                                  self.auth(), {"work": [work_item()]}])
        original = client.tool_call
        locks = []

        def request(name, args, timeout=30):
            locks.append((self.state / "run.lock").read_text())
            return original(name, args, timeout)

        with patch.object(client, "tool_call", side_effect=request):
            self.assertEqual(self.run_cli(client, "listen", "--budget", "100"), 0)
        polls = [args for name, args, _ in client.calls if name == "poll_work"]
        self.assertEqual(len(polls), 3)
        self.assertEqual(polls[1], polls[2])
        self.assertEqual(polls[2]["cursor"], "saved")
        self.assertEqual(len(set(locks)), 1)
        self.sleep.assert_called_once_with(15)
        self.assertFalse((self.state / "auth_failed").exists())
        self.assertEqual((self.state / "auth_blips").read_text(), "1000\n")
        self.assertEqual(json.loads((self.state / "run.lock").read_text()),
                         self.handling_of())

    def test_three_auth_failures_pause(self):
        client = FakeClient({"get_self": self.auth()}, polls=[])
        self.assertEqual(self.run_cli(client, "listen", "--budget", "100"), 2)
        self.assertEqual(sum(n == "get_self" for n, _, _ in client.calls), 3)
        self.assertEqual(self.sleep.call_args_list, [call(15), call(60)])
        self.assertTrue((self.state / "auth_failed").exists())
        self.assertFalse((self.state / "run.lock").exists())

    def test_reserved_rpc_immediately_pauses(self):
        client = filament.Client()
        response = json.dumps({"jsonrpc": "2.0", "error": {
            "code": -32002, "message": "reserved"}}).encode()
        with patch.object(client, "_raw_post", return_value=response):
            self.assertEqual(self.run_cli(client, "listen", "--budget", "100"), 2)
        self.sleep.assert_not_called()
        self.assertTrue((self.state / "auth_failed").exists())

    def test_non_listener_401_does_not_pause(self):
        client = FakeClient({"get_self": self.auth()})
        self.assertEqual(self.run_cli(client, "self"), 2)
        self.sleep.assert_not_called()
        self.assertFalse((self.state / "auth_failed").exists())
        self.assertEqual((self.state / "auth_blips").read_text(), "1000\n")

    def test_backstop_yields_before_first_request(self):
        self.wanted()
        client = FakeClient()
        self.assertEqual(self.run_cli(client, "listen", "--budget", "100",
                                      "--role", "backstop"), 3)
        self.assertEqual(json.loads(self.stdout.getvalue()), {"work": []})
        self.assertEqual(client.calls, [])
        self.assertFalse((self.state / "run.lock").exists())
        # The marker belongs to the front door that wrote it.
        self.assertTrue((self.state / "frontdoor.wanted").exists())

    def test_backstop_yields_before_next_poll(self):
        client = FakeClient(polls=[{"work": []}])

        def sleep(seconds):
            self.advance(seconds)
            self.wanted()

        self.sleep.side_effect = sleep
        self.assertEqual(self.run_cli(client, "listen", "--budget", "100",
                                      "--role", "backstop"), 3)
        self.assertEqual(sum(n == "poll_work" for n, _, _ in client.calls), 1)
        self.assertFalse((self.state / "run.lock").exists())

    def test_frontdoor_requests_and_takes_handoff(self):
        self.lock()
        client = FakeClient(polls=[{"work": [work_item()]}])

        def release(seconds):
            self.assertEqual(seconds, 2)
            self.assertTrue((self.state / "frontdoor.wanted").exists())
            self.advance(seconds)
            (self.state / "run.lock").unlink()

        self.sleep.side_effect = release
        original = client.handshake

        def handshake():
            self.assertEqual(json.loads((self.state / "run.lock").read_text()),
                             {"start": 1000, "role": "frontdoor"})
            self.assertFalse((self.state / "frontdoor.wanted").exists())
            original()

        with patch.object(client, "handshake", side_effect=handshake):
            self.assertEqual(self.run_cli(client, "listen", "--budget", "100"), 0)
        self.sleep.assert_called_once_with(2)

    def test_frontdoor_never_takes_live_frontdoor_even_same_start(self):
        self.lock("frontdoor")
        for start in ("900", "1000"):
            with patch.dict(os.environ, FILAMENT_RUN_START=start):
                self.assertEqual(self.run_cli(FakeClient(), "listen", "--budget", "100"), 3)
        self.sleep.assert_not_called()
        self.assertTrue((self.state / "run.lock").exists())

    def test_ensure_reports_roles_and_legacy_lock(self):
        for role, legacy in (("backstop", False), ("frontdoor", False), ("frontdoor", True)):
            with self.subTest(role=role, legacy=legacy):
                self.lock(role, legacy)
                self.stdout.seek(0)
                self.stdout.truncate()
                self.assertEqual(self.run_cli(FakeClient(), "ensure"), 0)
                self.assertEqual(json.loads(self.stdout.getvalue()),
                                 {"listener": "alive", "role": role})
                self.assertEqual(filament._lock_info()[0]["start"], "900")

    def test_deadline_before_first_or_second_probe(self):
        for budget, expected_calls, marker in ((10, 1, False), (50, 2, True)):
            with self.subTest(budget=budget):
                client = FakeClient({"get_self": self.auth()})
                self.assertEqual(self.run_cli(client, "listen", "--budget", str(budget)), 2)
                self.assertEqual(len(client.calls), expected_calls)
                self.assertEqual((self.state / "auth_failed").exists(), marker)
                self.assertLess(self.now, 1000 + budget)

    def test_non_auth_probe_failure_does_not_pause(self):
        client = FakeClient()
        with patch.object(client, "handshake", side_effect=[self.auth(), None]), patch.object(
                client, "tool_call", side_effect=filament.FilamentError("server unavailable")):
            self.assertEqual(self.run_cli(client, "listen", "--budget", "20"), 3)
        self.assertFalse((self.state / "auth_failed").exists())

    def test_final_poll_auth_is_not_swallowed(self):
        client = FakeClient(polls=[self.auth()])
        self.assertEqual(self.run_cli(client, "listen", "--budget", "10"), 2)
        self.sleep.assert_not_called()
        self.assertFalse((self.state / "auth_failed").exists())

    def test_second_probe_success_recovers(self):
        client = FakeClient(polls=[self.auth(), {"work": [work_item()]}])
        original = client.tool_call
        identities = iter([{}, self.auth(), {}])

        def request(name, args, timeout=30):
            if name == "get_self":
                result = next(identities)
                if isinstance(result, Exception):
                    raise result
            return original(name, args, timeout)

        with patch.object(client, "tool_call", side_effect=request):
            self.assertEqual(self.run_cli(client, "listen", "--budget", "100"), 0)
        self.assertEqual(self.sleep.call_args_list, [call(15), call(60)])
        self.assertFalse((self.state / "auth_failed").exists())

    def test_stale_wanted_does_not_stop_backstop(self):
        self.wanted(age=120)
        client = FakeClient(polls=[{"work": [work_item()]}])
        self.assertEqual(self.run_cli(client, "listen", "--budget", "100",
                                      "--role", "backstop"), 0)
        self.assertFalse((self.state / "frontdoor.wanted").exists())

    def test_blips_bounded_and_reset(self):
        (self.state / "auth_blips").write_text("\n".join(str(i) for i in range(25)))
        self.assertEqual(self.run_cli(FakeClient({"get_self": self.auth()}), "self"), 2)
        lines = (self.state / "auth_blips").read_text().splitlines()
        self.assertEqual(lines, [str(i) for i in range(6, 25)] + ["1000"])
        self.wanted()
        self.assertEqual(self.run_cli(FakeClient(), "reset"), 0)
        self.assertFalse((self.state / "auth_blips").exists())
        self.assertFalse((self.state / "frontdoor.wanted").exists())

    def test_request_refused_after_good_probe_pauses(self):
        client = FakeClient(polls=[self.auth(), self.auth()])
        self.assertEqual(self.run_cli(client, "listen", "--budget", "100"), 2)
        self.assertEqual(sum(n == "poll_work" for n, _, _ in client.calls), 2)
        self.sleep.assert_called_once_with(15)
        self.assertTrue((self.state / "auth_failed").exists())
        self.assertFalse((self.state / "run.lock").exists())

    def test_probe_network_error_waits_for_next_probe(self):
        client = FakeClient(polls=[self.auth(), {"work": [work_item()]}])
        original = client.tool_call
        identities = iter([{}, filament.urllib.error.URLError("down"), {}])

        def request(name, args, timeout=30):
            if name == "get_self":
                result = next(identities)
                if isinstance(result, Exception):
                    raise result
            return original(name, args, timeout)

        with patch.object(client, "tool_call", side_effect=request):
            self.assertEqual(self.run_cli(client, "listen", "--budget", "100"), 0)
        self.assertEqual(self.sleep.call_args_list, [call(15), call(60)])
        self.assertFalse((self.state / "auth_failed").exists())

    def test_backstop_yields_during_auth_probe(self):
        client = FakeClient(polls=[self.auth()])

        def sleep(seconds):
            self.assertLessEqual(seconds, 2)
            self.advance(seconds)
            self.wanted()

        self.sleep.side_effect = sleep
        self.assertEqual(self.run_cli(client, "listen", "--budget", "100",
                                      "--role", "backstop"), 3)
        self.assertEqual(json.loads(self.stdout.getvalue()), {"work": []})
        self.assertFalse(any(n == "get_self" for n, _, _ in client.calls[1:]))
        self.assertFalse((self.state / "run.lock").exists())
        self.assertFalse((self.state / "auth_failed").exists())

    def test_stale_lock_claimed_by_another_starter_is_left_alone(self):
        self.lock()
        self.advance(filament.LOCK_ALIVE_SECONDS)

        def other_starter_claims(fd, op):
            # Another starter cleared the stale lock and took it while this
            # one waited for the guard.
            self.lock("frontdoor")

        with patch.object(filament.fcntl, "flock", side_effect=other_starter_claims):
            self.assertEqual(self.run_cli(FakeClient(), "listen", "--budget", "100"), 3)
        self.assertEqual(filament._lock_info()[0], {"start": "900", "role": "frontdoor"})

    def test_failed_turn_gets_its_work_back_next_run(self):
        client = FakeClient(polls=[{"work": [], "cursor": "c:1"},
                                   {"work": [work_item()], "cursor": "c:2"}])
        self.assertEqual(self.run_cli(client, "listen", "--budget", "100"), 0)
        self.assertEqual((self.state / "cursor").read_text(), "c:1")

        # The turn never replied; the next run resumes from before the delivery.
        client = FakeClient(polls=[{"work": [work_item()], "cursor": "c:2"}])
        self.assertEqual(self.run_cli(client, "listen", "--budget", "100"), 0)
        polls = [args for name, args, _ in client.calls if name == "poll_work"]
        self.assertEqual(polls[0]["cursor"], "c:1")
        self.assertEqual((self.state / "cursor").read_text(), "c:1")

    def test_cursor_advances_past_empty_polls(self):
        (self.state / "cursor").write_text("c:5")
        client = FakeClient(polls=[{"work": [], "cursor": "c:6"},
                                   {"work": [work_item()], "cursor": "c:7"}])
        self.assertEqual(self.run_cli(client, "listen", "--budget", "100"), 0)
        polls = [args for name, args, _ in client.calls if name == "poll_work"]
        self.assertEqual([p["cursor"] for p in polls], ["c:5", "c:6"])
        self.assertEqual((self.state / "cursor").read_text(), "c:6")

    def test_first_run_polls_without_cursor(self):
        client = FakeClient(polls=[{"work": [work_item()], "cursor": "c:2"}])
        self.assertEqual(self.run_cli(client, "listen", "--budget", "100"), 0)
        polls = [args for name, args, _ in client.calls if name == "poll_work"]
        self.assertNotIn("cursor", polls[0])
        self.assertFalse((self.state / "cursor").exists())

    def test_call_drops_null_arguments(self):
        client = FakeClient()
        self.assertEqual(self.run_cli(client, "call", "get_recent_messages",
                                      '{"channel": "room", "cursor": null}'), 0)
        self.assertEqual(client.calls[0][:2], ("get_recent_messages", {"channel": "room"}))

    def test_handoff_timeout_cleans_wanted_preserves_lock(self):
        self.lock()
        self.assertEqual(self.run_cli(FakeClient(), "listen", "--budget", "100"), 3)
        self.assertEqual(self.now, 1060)
        self.assertTrue((self.state / "run.lock").exists())
        self.assertFalse((self.state / "frontdoor.wanted").exists())


if __name__ == "__main__":
    unittest.main()
