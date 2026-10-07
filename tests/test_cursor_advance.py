import contextlib
import io
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
        return json.loads((self.state / 'last_delivery.json').read_text())

    def deliver(self, budget=100):
        items = [work_item(messages=[{'event_id': 'greeting'}, {'event_id': 'question'}]),
                 work_item(messages=[{'event_id': 'other'}])]
        client = FakeClient(polls=[{'work': items, 'cursor': 'delivered'}])
        self.assertEqual(filament._listen_loop(client, 1000 + budget, 30), 0)

    def reply(self, ids, client=None):
        return filament.cmd_reply(client or FakeClient(), json.dumps(work_item()['reply_with']),
                                  'Answer', ids)

    def test_delivery_records_all_ids_in_both_poll_branches(self):
        for budget in (100, 10):
            with self.subTest(budget=budget):
                self.deliver(budget)
                self.assertEqual(self.pending(), {'cursor': 'delivered',
                                                 'ids': ['greeting', 'question', 'other']})
                self.assertEqual(filament._load_cursor(), 'before')

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
        with self.assertRaises(filament.FilamentError):
            self.reply('greeting,question,other', FakeClient({
                'post_message': filament.FilamentError('failed')}))
        self.assertEqual(len(self.pending()['ids']), 3)
        self.assertEqual(filament._load_cursor(), 'before')

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

    def test_reset_removes_delivery(self):
        self.deliver()
        self.assertEqual(filament.main(['reset']), 0)
        self.assertFalse((self.state / 'last_delivery.json').exists())


if __name__ == '__main__':
    unittest.main()
