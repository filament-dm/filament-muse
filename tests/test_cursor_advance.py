import contextlib
import io
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_context import FakeClient, filament, work_item


class CursorAdvanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(filament, 'STATE_DIR', str(self.state)))
        stack.enter_context(patch.object(filament.time, 'time', return_value=1000))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        stack.enter_context(patch.object(filament.urllib.request, 'urlopen',
                                        side_effect=AssertionError('unexpected network request')))
        filament._members_cache.clear()
        self.addCleanup(filament._members_cache.clear)
        filament._save_cursor('before')

    def pending(self):
        delivery = json.loads((self.state / 'last_delivery.json').read_text())
        delivery.pop('since', None)
        return delivery

    def deliver(self, budget=100):
        items = [work_item(messages=[{'event_id': 'greeting'}, {'event_id': 'question'}]),
                 work_item(messages=[{'event_id': 'other'}])]
        for item in items:
            item['reply_with']['args']['in_reply_to'] = item['messages'][-1]['event_id']
        client = FakeClient(polls=[{'work': items, 'cursor': 'delivered'}])
        self.assertEqual(filament._listen_loop(client, 1000 + budget, 30), 0)

    def reply(self, ids, client=None):
        reply_with = work_item()['reply_with']
        reply_with['args']['in_reply_to'] = ids.split(',')[-1]
        return filament.cmd_reply(client or FakeClient(), json.dumps(reply_with), 'Answer', ids)

    def test_delivery_records_all_ids_in_both_poll_branches(self):
        for budget in (100, 10):
            with self.subTest(budget=budget):
                self.deliver(budget)
                self.assertEqual(self.pending(), {'cursor': 'delivered',
                                                 'ids': ['greeting', 'question', 'other']})
                self.assertEqual(filament._load_cursor(), 'before')

    def test_delivery_carries_poll_and_handoff_times(self):
        for budget in (100, 10):
            with self.subTest(budget=budget):
                out = io.StringIO()
                items = [work_item(messages=[{'event_id': 'q'}])]
                client = FakeClient(polls=[{'work': items, 'cursor': 'delivered'}])
                with contextlib.redirect_stdout(out):
                    self.assertEqual(filament._listen_loop(client, 1000 + budget, 30), 0)
                self.assertEqual(json.loads(out.getvalue())['timing'],
                                 {'polled_at': 1000, 'delivered_at': 1000})

    def test_partial_then_complete_reply(self):
        self.deliver()
        self.assertEqual(self.reply('greeting,question'), 0)
        self.assertEqual(self.pending(), {'cursor': 'delivered', 'ids': ['other']})
        self.assertEqual(filament._load_cursor(), 'before')
        self.assertEqual(self.reply('other'), 0)
        self.assertEqual(filament._load_cursor(), 'delivered')
        self.assertFalse((self.state / 'last_delivery.json').exists())
        client = FakeClient(polls=[{'work': []}])
        self.assertEqual(filament._listen_loop(client, 1010, 30), 3)
        self.assertEqual(client.calls[-1][1]['cursor'], 'delivered')

    def test_partial_then_complete_ack(self):
        self.deliver()
        self.assertEqual(filament.cmd_ack(FakeClient(polls=[{}]), ['greeting']), 0)
        self.assertEqual(self.pending()['ids'], ['question', 'other'])
        self.assertEqual(filament._load_cursor(), 'before')
        self.assertEqual(filament.cmd_ack(FakeClient(polls=[{}]), ['question', 'other']), 0)
        self.assertEqual(filament._load_cursor(), 'delivered')
        self.assertFalse((self.state / 'last_delivery.json').exists())

    def test_unrelated_reply_and_ack_leave_delivery_untouched(self):
        self.deliver()
        original = (self.state / 'last_delivery.json').read_bytes()
        self.reply('unrelated')
        filament.cmd_ack(FakeClient(polls=[{}]), ['another'])
        self.assertEqual((self.state / 'last_delivery.json').read_bytes(), original)
        self.assertEqual(filament._load_cursor(), 'before')

    def test_duplicate_reply_ack_completes_delivery(self):
        self.deliver()
        filament._record_replied(['greeting', 'question', 'other'])
        self.assertEqual(self.reply('greeting,question,other', FakeClient(polls=[{}])), 0)
        self.assertEqual(filament._load_cursor(), 'delivered')
        self.assertFalse((self.state / 'last_delivery.json').exists())

    def test_failed_reply_does_not_complete_delivery(self):
        self.deliver()
        rpc_client = filament.Client()
        rejection = json.dumps({'jsonrpc': '2.0', 'error': {
            'code': -32603, 'message': 'Reply rejected'}}).encode()
        with patch.object(rpc_client, '_raw_post', return_value=rejection):
            with self.assertRaises(filament.FilamentError) as caught:
                rpc_client.tool_call('post_message', {}, timeout=30)
        with self.assertRaises(filament.FilamentError):
            self.reply('greeting,question,other', FakeClient({
                'post_message': caught.exception}))
        self.assertEqual(self.pending()['ids'], ['greeting', 'question', 'other'])
        self.assertTrue({'greeting', 'question', 'other'}.isdisjoint(
            filament._load_replied()))
        self.assertEqual(filament._load_cursor(), 'before')

    def test_rejected_reply_restores_answerability(self):
        self.deliver()
        original = (self.state / 'last_delivery.json').read_bytes()
        rpc_client = filament.Client()
        rejection = json.dumps({'jsonrpc': '2.0', 'error': {
            'code': -32603, 'message': 'Reply rejected'}}).encode()
        with patch.object(rpc_client, '_raw_post', return_value=rejection):
            with self.assertRaises(filament.FilamentError) as caught:
                rpc_client.tool_call('post_message', {}, timeout=30)
        with self.assertRaises(filament.FilamentError):
            self.reply('greeting,question', FakeClient({'post_message': caught.exception}))
        self.assertNotIn('question', filament._load_replied())
        self.assertNotIn('greeting', filament._load_replied())
        self.assertEqual((self.state / 'last_delivery.json').read_bytes(), original)
        self.deliver()
        client = FakeClient()
        self.reply('greeting,question', client)
        self.assertEqual(client.calls[-1][0], 'post_message')
        self.assertEqual(client.calls[-1][1]['in_reply_to'], 'question')
        self.assertEqual(self.pending()['ids'], ['other'])

    def test_http_auth_refusal_keeps_reply_answerable(self):
        self.deliver()
        original = (self.state / 'last_delivery.json').read_bytes()
        rpc_client = filament.Client()
        refused = filament.urllib.error.HTTPError('https://x', 401, 'Unauthorized', {}, None)
        with patch.object(filament.urllib.request, 'urlopen', side_effect=refused):
            with self.assertRaises(filament.FilamentError) as caught:
                rpc_client.tool_call('post_message', {}, timeout=30)
        self.assertTrue(caught.exception.auth)
        with self.assertRaises(filament.FilamentError):
            self.reply('greeting,question', FakeClient({'post_message': caught.exception}))
        self.assertTrue({'greeting', 'question'}.isdisjoint(filament._load_replied()))
        self.assertEqual((self.state / 'last_delivery.json').read_bytes(), original)

    def test_unknown_reply_outcome_keeps_duplicate_protection(self):
        self.deliver()
        with self.assertRaises(filament.FilamentError):
            self.reply('greeting,question', FakeClient({'post_message': TimeoutError('timeout')}))
        self.assertTrue({'greeting', 'question'} <= filament._load_replied().keys())
        self.assertEqual(self.pending(), {'cursor': 'delivered', 'ids': ['other']})
        self.assertEqual(filament._load_cursor(), 'before')

    def test_mixed_old_new_rejection_preserves_old_ids(self):
        self.deliver()
        filament._record_replied(['greeting'])
        original = self.pending()
        with self.assertRaises(filament.FilamentError):
            self.reply('greeting,question', FakeClient({
                'post_message': filament.FilamentError('rejected', rejected=True)}))
        self.assertIn('greeting', filament._load_replied())
        self.assertNotIn('question', filament._load_replied())
        self.assertEqual(self.pending(), original)
        self.assertEqual(filament._load_cursor(), 'before')

    def test_unknown_outcome_clears_pending_and_allows_advancement(self):
        for error in (TimeoutError('timeout'), ConnectionResetError('reset'),
                      filament.FilamentError('outcome unknown')):
            with self.subTest(error=type(error).__name__):
                filament._save_replied({})
                filament._save_cursor('before')
                self.deliver()
                with self.assertRaises(filament.FilamentError):
                    self.reply('greeting,question', FakeClient({'post_message': error}))
                self.assertTrue({'greeting', 'question'} <= filament._load_replied().keys())
                self.assertEqual(self.pending()['ids'], ['other'])
                self.assertEqual(filament._load_cursor(), 'before')
                with self.assertRaises(filament.FilamentError):
                    self.reply('other', FakeClient({'post_message': error}))
                self.assertIn('other', filament._load_replied())
                self.assertFalse((self.state / 'last_delivery.json').exists())
                self.assertEqual(filament._load_cursor(), 'delivered')

    def test_null_reply_is_consumed_at_delivery(self):
        for budget in (10, 100):
            with self.subTest(budget=budget):
                client = FakeClient(polls=[{'work': [work_item(reply_with=None)],
                                           'cursor': 'consumed'}])
                self.assertEqual(filament._listen_loop(client, 1000 + budget, 30), 0)
                self.assertEqual(filament._load_cursor(), 'consumed')
                self.assertFalse((self.state / 'last_delivery.json').exists())

    def test_second_delivery_unions_pending_ids(self):
        self.deliver()
        filament._prepare_delivery({'work': [work_item(messages=[
            {'event_id': 'question'}, {'event_id': 'new'}]),
            work_item(messages=[{'event_id': 'other'}], reply_with=None)],
            'cursor': 'newest'})
        self.assertEqual(self.pending(), {'cursor': 'newest',
                                         'ids': ['greeting', 'question', 'new']})
        self.assertEqual(filament._load_cursor(), 'before')
        filament.cmd_ack(FakeClient(polls=[{}]), ['greeting', 'question', 'new'])
        self.assertEqual(filament._load_cursor(), 'newest')

    def test_empty_and_filtered_polls_do_not_advance_pending_cursor(self):
        self.deliver()
        original = self.pending()
        filament._record_replied(['greeting', 'question', 'other'])
        for filtered in (False, True):
            for budget in (10, 100):
                with self.subTest(filtered=filtered, budget=budget):
                    work = [work_item(messages=[{'event_id': 'question'}])] if filtered else []
                    polls = [{'work': work, 'cursor': 'unsafe', 'truncated': True}]
                    if budget == 100:
                        polls.append({'work': [], 'cursor': 'also-unsafe'})
                    client = FakeClient(polls=polls)
                    times = [1000, 1095] if budget == 100 else [1000, 1000]
                    clock = itertools.chain(times, itertools.repeat(times[-1]))
                    with patch.object(filament.time, 'time', side_effect=lambda: next(clock)), \
                            patch.object(filament.time, 'sleep') as sleep:
                        self.assertEqual(filament._listen_loop(client, 1000 + budget, 30), 3)
                    if budget == 100:
                        # The held-back cursor would return the same page, so
                        # a truncated result waits instead of re-polling at once.
                        sleep.assert_called()
                    self.assertEqual(filament._load_cursor(), 'before')
                    self.assertEqual(self.pending(), original)
                    self.assertTrue(all(args['cursor'] == 'before'
                                        for tool, args, _ in client.calls if tool == 'poll_work'))

    def test_answered_messages_are_annotated_in_both_poll_branches(self):
        filament._record_replied(['old', 'history'])
        for budget in (100, 10):
            with self.subTest(budget=budget):
                item = work_item(messages=[{'event_id': 'old'}, {'event_id': 'new'}])
                client = FakeClient({'get_recent_messages': {'messages': [
                    {'event_id': 'history'}]}}, polls=[{'work': [item], 'cursor': 'new-cursor'}])
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(filament._listen_loop(client, 1000 + budget, 30), 0)
                delivered = json.loads(output.getvalue())['work'][0]
                for field in ('messages', 'context'):
                    by_id = {m['event_id']: m for m in delivered[field]}
                    self.assertTrue(by_id['old']['already_answered'])
                    self.assertNotIn('already_answered', by_id['new'])
                self.assertTrue(delivered['context'][0]['already_answered'])
                self.assertEqual(self.pending()['ids'], ['old', 'new'])

    def test_failed_ack_still_releases_the_cursor(self):
        self.deliver()
        down = filament.FilamentError('poll_work ack: network error')
        with patch.object(filament.time, 'sleep'), self.assertRaises(filament.FilamentError):
            filament.cmd_ack(FakeClient(polls=[down] * 4), ['greeting', 'question', 'other'])
        self.assertEqual(filament._load_cursor(), 'delivered')
        self.assertFalse((self.state / 'last_delivery.json').exists())

    def test_failed_duplicate_guard_ack_still_releases_the_cursor(self):
        self.deliver()
        filament._record_replied(['greeting', 'question', 'other'])
        down = filament.FilamentError('poll_work ack: network error')
        with patch.object(filament.time, 'sleep'), self.assertRaises(filament.FilamentError):
            self.reply('greeting,question,other', FakeClient(polls=[down] * 4))
        self.assertEqual(filament._load_cursor(), 'delivered')

    def test_stale_pending_delivery_is_given_up(self):
        self.deliver()
        self.assertEqual(json.loads((self.state / 'last_delivery.json').read_text())['since'], 1000)
        with patch.object(filament.time, 'time',
                          return_value=1000 + filament.PENDING_DELIVERY_SECONDS + 1):
            filament._save_poll_cursor('later')
        self.assertEqual(filament._load_cursor(), 'later')
        self.assertFalse((self.state / 'last_delivery.json').exists())

    def test_second_delivery_keeps_the_oldest_since(self):
        self.deliver()
        with patch.object(filament.time, 'time', return_value=2000):
            filament._prepare_delivery({'work': [work_item(messages=[{'event_id': 'new'}])],
                                        'cursor': 'newest'})
        self.assertEqual(json.loads((self.state / 'last_delivery.json').read_text())['since'], 1000)

    def test_damaged_delivery_file_is_dropped(self):
        for text in ('', '{"cursor": "x"}', '[1, 2]'):
            with self.subTest(text=text):
                (self.state / 'last_delivery.json').write_text(text)
                filament._save_poll_cursor('fresh')
                self.assertEqual(filament._load_cursor(), 'fresh')
                self.assertFalse((self.state / 'last_delivery.json').exists())

    def test_reset_removes_delivery(self):
        self.deliver()
        self.assertEqual(filament.main(['reset']), 0)
        self.assertFalse((self.state / 'last_delivery.json').exists())


if __name__ == '__main__':
    unittest.main()
