import contextlib
import copy
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import call, patch


dc = types.ModuleType("dynamic_credentials")
dc.add_surrogate_to_request = lambda *args, **kwargs: None
dc.read_response_body = lambda response: response.read()
dc.DynamicCredentialError = type("DynamicCredentialError", (Exception,), {})
loader = SourceFileLoader(
    "filament_context_test", str(Path(__file__).resolve().parents[1] / "skill/bin/filament")
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
    def __init__(self, results=None, polls=()):
        self.results = results or {}
        self.polls = list(polls)
        self.calls = []
        self.handshakes = 0
        self.heartbeats = 0

    def handshake(self):
        self.handshakes += 1

    def heartbeat(self, timeout=30):
        self.heartbeats += 1

    def tool_call(self, name, arguments, timeout=30):
        self.calls.append((name, copy.deepcopy(arguments), timeout))
        if name == "poll_work":
            if not self.polls:
                raise AssertionError("unexpected extra poll")
            result = self.polls.pop(0)
        else:
            result = self.results.get(name, {})
        if isinstance(result, Exception):
            raise result
        return {"content": [{"type": "text", "text": json.dumps(result)}]}


def work_item(**overrides):
    item = {
        "channel_id": "room", "is_backchannel": False,
        "messages": [{"event_id": "own", "body": "Question?", "ts": 10}],
        "reply_with": {"tool": "post_message", "args": {
            "channel": "room", "in_reply_to": "own",
        }},
    }
    item.update(overrides)
    return item


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name) / "state"
        self.now = 1000.0
        contexts = contextlib.ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(patch.object(filament, "STATE_DIR", str(self.state_dir)))
        contexts.enter_context(patch.object(filament.time, "time", side_effect=lambda: self.now))
        self.sleep = contexts.enter_context(patch.object(
            filament.time, "sleep", side_effect=self.advance,
        ))
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        contexts.enter_context(contextlib.redirect_stdout(self.stdout))
        contexts.enter_context(contextlib.redirect_stderr(self.stderr))
        contexts.enter_context(patch.object(
            filament.urllib.request, "urlopen",
            side_effect=AssertionError("unexpected network request"),
        ))

    def advance(self, seconds):
        self.now += seconds

    def listen(self, client, budget=100):
        self.stdout.seek(0)
        self.stdout.truncate()
        code = filament._listen_loop(client, self.now + budget, 30)
        return code, json.loads(self.stdout.getvalue())

    def test_room_context_sorted_filtered_merged_and_media_copied(self):
        media = [{"url": "mxc://room/image", "filename": "image.png"}]
        messages = [
            {"event_id": "own", "body": "stale", "ts": 20, "media": media,
             "sender": "Alice", "unwanted": True},
            {"event_id": "first", "body": "older", "ts": 1,
             "is_from_self": False, "is_from_principal": True,
             "is_from_agent": False, "thread_id": "root", "unwanted": True},
            {"event_id": "first", "body": "updated", "ts": 1},
        ]
        client = FakeClient({"get_recent_messages": {
            "messages": messages, "cursor": "older-page",
        }})
        item = work_item()
        filament._enrich_items(client, [item], self.now + 100)
        self.assertEqual(client.calls, [
            ("get_recent_messages", {"channel": "room", "limit": 40}, 30),
        ])
        self.assertEqual(item["context"], [
            {"event_id": "first", "body": "updated", "ts": 1,
             "is_from_self": False, "is_from_principal": True,
             "is_from_agent": False, "thread_id": "root"},
            {"event_id": "own", "body": "Question?", "ts": 10,
             "media": media, "sender": "Alice"},
        ])
        self.assertEqual(item["context_cursor"], "older-page")
        self.assertEqual(item["messages"][0]["media"], media)
        self.assertNotIn("thread", item)
        self.assertNotIn("context_error", item)

    def test_fetched_limit_does_not_exclude_own_messages(self):
        for backchannel, limit in ((False, 40), (True, 100)):
            with self.subTest(backchannel=backchannel):
                history = [{"event_id": str(i), "ts": i} for i in range(limit)]
                client = FakeClient({"get_recent_messages": {"messages": history}})
                item = work_item(is_backchannel=backchannel)
                filament._enrich_items(client, [item], self.now + 100)
                self.assertEqual(client.calls[0][1]["limit"], limit)
                self.assertEqual(len(item["context"]), limit + 1)
                self.assertIn(item["messages"][0], item["context"])
                self.assertNotIn("context_cursor", item)

    def test_thread_sorted_newest_50_and_media_precedence(self):
        room_media = [{"url": "mxc://room/image"}]
        thread_media = [{"url": "mxc://thread/image"}]
        # The work message falls outside the thread cap; its media must survive.
        thread = [{"event_id": str(i), "ts": i, "extra": True}
                  for i in reversed(range(60))]
        thread.append({"event_id": "own", "ts": -1, "media": thread_media})
        client = FakeClient({
            "get_recent_messages": {"messages": [
                {"event_id": "own", "media": room_media},
            ]},
            "get_thread": {"messages": thread},
        })
        item = work_item(thread_id="root")
        filament._enrich_items(client, [item], self.now + 100)
        self.assertEqual(client.calls[1], ("get_thread", {"message_id": "root"}, 30))
        self.assertEqual(item["thread"], [
            {"event_id": str(i), "ts": i} for i in range(10, 60)
        ])
        self.assertEqual(item["messages"][0]["media"], thread_media)
        self.assertEqual(item["context"][0]["media"], room_media)

    def test_own_fields_win_before_media_copy(self):
        item = work_item(messages=[{
            "event_id": "own", "body": "", "ts": 0, "media": ["original"],
            "is_from_self": False,
        }])
        client = FakeClient({"get_recent_messages": {"messages": [{
            "event_id": "own", "body": "old", "ts": 1, "media": ["fetched"],
            "is_from_self": True,
        }]}})
        original = copy.deepcopy(item["messages"])
        filament._enrich_items(client, [item], self.now + 100)
        self.assertEqual(item["context"], original)
        self.assertEqual(item["messages"][0]["media"], ["fetched"])

    def test_room_and_thread_fail_independently(self):
        for room_fails, thread_fails in ((True, False), (False, True), (True, True)):
            with self.subTest(room_fails=room_fails, thread_fails=thread_fails):
                room = {"messages": [{"event_id": "own", "media": ["room"]}]}
                thread = {"messages": [{"event_id": "own", "media": ["thread"]}]}
                client = FakeClient({
                    "get_recent_messages": filament.FilamentError("boom") if room_fails else room,
                    "get_thread": filament.FilamentError("thread boom") if thread_fails else thread,
                })
                item = work_item(thread_id="root")
                items = [item]
                filament._enrich_items(client, items, self.now + 100)
                self.assertEqual(items, [item])
                self.assertIn("get_thread", [name for name, _, _ in client.calls])
                if room_fails:
                    self.assertEqual(item["context"], [])
                    self.assertEqual(item["context_error"], "boom")
                else:
                    self.assertEqual(item["context"][0]["body"], "Question?")
                    self.assertNotIn("context_error", item)
                if thread_fails:
                    self.assertEqual(item["thread"], [])
                    self.assertEqual(item["thread_error"], "thread boom")
                else:
                    self.assertEqual(item["thread"], thread["messages"])
                    self.assertNotIn("thread_error", item)
                expected_media = None if room_fails and thread_fails else (
                    ["room"] if thread_fails else ["thread"])
                self.assertEqual(item["messages"][0].get("media"), expected_media)
                self.assertIn("boom", self.stderr.getvalue())

    def test_auth_failure_from_either_read_propagates_without_retry(self):
        for name in ("get_recent_messages", "get_thread"):
            with self.subTest(tool=name):
                client = FakeClient({name: filament.FilamentError("x", auth=True)})
                with self.assertRaises(filament.FilamentError) as raised:
                    filament._enrich_items(client, [work_item(thread_id="root")], self.now + 100)
                self.assertTrue(raised.exception.auth)
                self.assertEqual(sum(n == name for n, _, _ in client.calls), 1)
                self.assertEqual(client.calls[-1][0], name)
        self.sleep.assert_not_called()

    def test_missing_channel_makes_no_read_even_with_thread(self):
        item = work_item(thread_id="root")
        del item["channel_id"]
        client = FakeClient()
        filament._enrich_items(client, [item], self.now + 100)
        self.assertEqual(item["context"], [])
        self.assertEqual(item["context_error"], "item has no channel_id")
        self.assertEqual(client.calls, [])

    def test_malformed_entries_and_timestamps_keep_stable_order(self):
        messages = [None, "bad", 7, [],
                    {"event_id": "later", "ts": 2.5},
                    {"body": "missing timestamp and id"},
                    {"event_id": "text", "ts": "99"},
                    {"event_id": "null", "ts": None},
                    {"event_id": "object", "ts": {}},
                    {"body": "another missing id", "ts": 0},
                    {"event_id": "first", "ts": -1}]
        expected = [messages[10], *messages[5:10], messages[4]]
        client = FakeClient({"get_recent_messages": messages, "get_thread": messages})
        item = work_item(messages=[None, "bad"], thread_id="root")
        filament._enrich_items(client, [None, "bad", item], self.now + 100)
        self.assertEqual(item["context"], expected)
        self.assertEqual(item["thread"], expected)
        self.assertEqual(item["messages"], [None, "bad"])

    def test_malformed_result_message_lists_are_empty(self):
        for result in ({"messages": None}, {"messages": "bad"}, None, 42):
            with self.subTest(result=result):
                client = FakeClient({"get_recent_messages": result, "get_thread": result})
                item = work_item(thread_id="root")
                filament._enrich_items(client, [item], self.now + 100)
                self.assertEqual(item["context"], item["messages"])
                self.assertEqual(item["thread"], [])

    def test_body_truncation_does_not_mutate_original_messages(self):
        messages = [{"event_id": str(i), "body": body}
                    for i, body in enumerate(("x" * 1999, "x" * 2000, "é" * 2001, None))]
        original = copy.deepcopy(messages)
        client = FakeClient({"get_recent_messages": {"messages": messages},
                             "get_thread": {"messages": messages}})
        item = work_item(messages=messages, thread_id="root")
        filament._enrich_items(client, [item], self.now + 100)
        for key in ("context", "thread"):
            self.assertEqual(item[key][:2], original[:2])
            self.assertEqual(item[key][2]["body"], "é" * 2000 + " […]")
            self.assertTrue(item[key][2]["truncated"])
            self.assertEqual(item[key][3], original[3])
        self.assertEqual(messages, original)

    def test_lock_touched_before_each_read_including_retries(self):
        client = FakeClient({"get_recent_messages": filament.FilamentError("boom")})
        events = []
        original_call = client.tool_call

        def recorded_call(name, arguments, timeout=30):
            events.append(name)
            return original_call(name, arguments, timeout)

        with patch.object(filament, "_touch_lock", side_effect=lambda: events.append("touch")), \
                patch.object(client, "tool_call", side_effect=recorded_call):
            filament._enrich_items(client, [work_item(thread_id="root")], self.now + 100)
        self.assertEqual(events, ["touch", "get_recent_messages"] * 4 + ["touch", "get_thread"])

    def test_both_listener_branches_preserve_work_and_reply_targets(self):
        for budget, poll_wait, enrichment_deadline in ((100, 30, 1100), (10, 0, 1020)):
            with self.subTest(budget=budget):
                item = work_item(thread_id="root")
                original_messages = json.dumps(item["messages"])
                original_reply = json.dumps(item["reply_with"])
                client = FakeClient({
                    "get_recent_messages": {"messages": [{"event_id": "history", "ts": 1}]},
                    "get_thread": {"messages": [{"event_id": "thread-history", "ts": 2}]},
                }, polls=[{"work": [item], "cursor": "poll-cursor", "truncated": False}])
                with patch.object(filament, "call_with_retry", wraps=filament.call_with_retry) as retry:
                    code, out = self.listen(client, budget)
                self.assertEqual(code, 0)
                printed = out["work"][0]
                self.assertEqual([m["event_id"] for m in printed["context"]], ["history", "own"])
                self.assertEqual(printed["thread"], [{"event_id": "thread-history", "ts": 2}])
                self.assertEqual(json.dumps(printed["messages"]), original_messages)
                self.assertEqual(json.dumps(printed["reply_with"]), original_reply)
                self.assertEqual(len(printed["messages"]), 1)
                self.assertEqual([m["event_id"] for m in printed["messages"]], ["own"])
                self.assertEqual(out["cursor"], "poll-cursor")
                self.assertNotIn("context_trimmed", out)
                polls = [args for name, args, _ in client.calls if name == "poll_work"]
                self.assertEqual(polls, [{"wait_seconds": poll_wait, "max_items": 10}])
                reads = [c.args for c in retry.call_args_list
                         if c.args[2] in ("get_recent_messages", "get_thread")]
                self.assertEqual([c[1] for c in reads], [enrichment_deadline] * 2)
                self.assertEqual(client.handshakes, 1)
                self.assertEqual(client.heartbeats, 1)
                self.assertEqual(filament._load_replied(), {})
                self.assertFalse(any(name in ("post_message", "reply_in_thread")
                                     for name, _, _ in client.calls))

    def test_listener_prints_work_after_read_failure_in_both_branches(self):
        for budget in (100, 10):
            with self.subTest(budget=budget):
                item = work_item()
                client = FakeClient({"get_recent_messages": filament.FilamentError("boom")},
                                    polls=[{"work": [item]}])
                code, out = self.listen(client, budget)
                self.assertEqual(code, 0)
                self.assertEqual(out["work"][0]["context"], [])
                self.assertEqual(out["work"][0]["context_error"], "boom")
                self.assertEqual(out["work"][0]["messages"], item["messages"])

    def test_listener_skips_non_dict_work_and_message_entries(self):
        item = work_item(messages=[None, "bad", {"event_id": "own"}])
        client = FakeClient(polls=[{"work": [None, "bad", item]}])
        code, out = self.listen(client)
        self.assertEqual(code, 0)
        self.assertEqual(len(out["work"]), 1)
        self.assertEqual(out["work"][0]["context"], [{"event_id": "own"}])
        self.assertEqual(out["work"][0]["messages"], item["messages"])

    def test_already_answered_work_is_filtered_and_only_its_ids_are_acked(self):
        filament._record_replied(["answered"])
        answered = work_item(channel_id="answered-room", messages=[{"event_id": "answered"}])
        client = FakeClient({"get_recent_messages": {"messages": [{"event_id": "history"}]}},
                            polls=[{"work": [answered], "cursor": "next", "truncated": True},
                                   {"work": [answered, work_item()]}])
        code, out = self.listen(client)
        self.assertEqual(code, 0)
        self.assertEqual(len(out["work"]), 1)
        self.assertEqual(out["work"][0]["messages"], work_item()["messages"])
        polls = [args for name, args, _ in client.calls if name == "poll_work"]
        self.assertNotIn("ack", polls[0])
        self.assertEqual(polls[1]["ack"], ["answered"])
        self.assertEqual(polls[1]["cursor"], "next")
        reads = [args for name, args, _ in client.calls if name == "get_recent_messages"]
        self.assertEqual(reads, [{"channel": "room", "limit": 40}])
        self.assertEqual(set(filament._load_replied()), {"answered"})
        self.sleep.assert_not_called()

    def test_answered_only_poll_keeps_five_second_floor_then_final_poll(self):
        filament._record_replied(["own"])
        client = FakeClient(polls=[
            {"work": [work_item()], "next_poll_ms": 1}, {"work": [work_item()]},
        ])
        code, out = self.listen(client, budget=19)
        self.assertEqual(code, 3)
        self.assertEqual(out, {"work": []})
        self.sleep.assert_called_once_with(5.0)
        polls = [args for name, args, _ in client.calls if name == "poll_work"]
        self.assertEqual(polls[1]["wait_seconds"], 0)
        self.assertEqual(polls[1]["ack"], ["own"])
        self.assertFalse(any(name == "get_recent_messages" for name, _, _ in client.calls))

    def test_output_trim_in_both_listener_branches_is_round_robin(self):
        for budget in (100, 10):
            with self.subTest(budget=budget):
                history = [{"event_id": str(i), "body": "é" * 3000, "ts": i}
                           for i in range(100)]
                items = [work_item(channel_id=str(i), is_backchannel=True) for i in range(3)]
                client = FakeClient({"get_recent_messages": {"messages": history}},
                                    polls=[{"work": items}])
                code, out = self.listen(client, budget)
                self.assertEqual(code, 0)
                self.assertTrue(out["context_trimmed"])
                self.assertLessEqual(len(json.dumps(out).encode("utf-8")), 400_000)
                self.assertLessEqual(len(self.stdout.getvalue().rstrip("\n").encode("utf-8")), 400_000)
                lengths = [len(item["context"]) for item in out["work"]]
                self.assertGreater(min(lengths), 0)
                self.assertLessEqual(max(lengths) - min(lengths), 1)
                self.assertEqual(lengths, sorted(lengths))
                for original, printed in zip(items, out["work"]):
                    self.assertEqual(json.dumps(printed["messages"]), json.dumps(original["messages"]))
                    self.assertEqual(json.dumps(printed["reply_with"]), json.dumps(original["reply_with"]))
                    ids = [m["event_id"] for m in printed["context"]]
                    self.assertEqual(ids, [str(i) for i in range(100 - len(ids), 100)])
                    self.assertTrue(all(m["truncated"] for m in printed["context"]))

    def test_trim_stops_when_only_original_work_exceeds_budget(self):
        item = work_item(messages=[{"event_id": "own", "body": "x" * 400_001}])
        item["context"] = [{"event_id": "history"}]
        item["thread"] = [{"event_id": "root"}]
        original = copy.deepcopy(item)
        out = {"work": [item]}
        filament._trim_context(out)
        self.assertEqual(item["context"], [])
        self.assertEqual(item["messages"], original["messages"])
        self.assertEqual(item["thread"], original["thread"])
        self.assertEqual(item["reply_with"], original["reply_with"])
        self.assertTrue(out["context_trimmed"])


if __name__ == "__main__":
    unittest.main()
