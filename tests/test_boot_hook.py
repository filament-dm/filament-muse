import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


HOOK = Path(__file__).resolve().parents[1] / 'skill/hooks/filament-boot.sh'


@unittest.skipUnless(shutil.which('jq'), 'jq is required for hook tests')
class BootHookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runtime = self.root / 'runtime.sh'
        self.runtime.write_text('''
silent() { printf 'silent:%s\\n' "$1"; exit 0; }
wake() { printf 'wake:%s\\n' "$2"; }
hook_state_get() { printf '%s' "$TEST_STATE"; }
hook_state_set() { printf 'state:%s\\n' "$1"; }
cat() {
    case "$1" in
        /proc/sys/kernel/random/boot_id) printf '%s' "$TEST_BOOT" ;;
        /proc/uptime) printf '%s.00 0.00' "$TEST_UPTIME" ;;
    esac
}
date() { printf '%s' "${TEST_NOW:-10000}"; }
''')
        self.cli = self.root / 'workspace/skills/filament/bin/filament'
        self.cli.parent.mkdir(parents=True)
        self.cli.write_text('#!/bin/bash\nprintf "%s" "$TEST_ENSURE"\nexit "${TEST_EXIT:-0}"\n')
        self.cli.chmod(0o755)

    def run_hook(self, **env):
        values = dict(os.environ, HOME=str(self.root), HATCH_HOOK_RUNTIME=str(self.runtime),
                      TEST_STATE='{}', TEST_BOOT='boot-one', TEST_UPTIME='60',
                      TEST_ENSURE='{"listener":"none"}')
        values.update(env)
        result = subprocess.run(['/bin/bash', str(HOOK)], env=values,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def wake(self, lines):
        """The (state, payload) of a run that woke, or fail."""
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].startswith('state:'), lines)
        self.assertTrue(lines[1].startswith('wake:'), lines)
        return json.loads(lines[0][6:]), json.loads(lines[1][5:])

    def test_after_boot_any_status_but_a_live_frontdoor_wakes(self):
        # 'handling' and 'unknown' without a role are defensive fixtures.
        for status in ({'listener': 'none'}, {'listener': 'alive', 'role': 'backstop'},
                       {'listener': 'handling', 'role': 'backstop'},
                       {'listener': 'handling', 'role': 'frontdoor'},
                       {'listener': 'alive'}, {'listener': 'handling'},
                       {'listener': 'unknown'}):
            state, payload = self.wake(self.run_hook(TEST_ENSURE=json.dumps(status)))
            self.assertEqual(payload['ensure'], status)
            self.assertEqual(payload['event'], 'vm_replaced')
            self.assertEqual(state['wake_boot_id'], 'boot-one')
            self.assertEqual(state['woke_for'], 10000)
        self.assertTrue(self.run_hook(TEST_ENSURE=json.dumps(
            {'listener': 'alive', 'role': 'frontdoor'}))[0].startswith('silent:'))

    def test_paused_listener_never_wakes(self):
        for uptime in ('60', '5000'):
            self.assertEqual(self.run_hook(TEST_UPTIME=uptime, TEST_ENSURE='{"listener":"paused"}'),
                             ['silent:listener paused on auth failure'])

    def test_invalid_ensure_or_state_never_consumes_wake(self):
        for env in ({'TEST_ENSURE': 'broken'}, {'TEST_ENSURE': '{}\n{}'},
                    {'TEST_ENSURE': '[]'}, {'TEST_STATE': 'broken'},
                    {'TEST_EXIT': '1'}):
            self.assertEqual(len(self.run_hook(**env)), 1)
            self.assertTrue(self.run_hook(**env)[0].startswith('silent:'))

    def test_missing_dependencies_are_silent(self):
        self.assertEqual(self.run_hook(PATH=str(self.root)), ['silent:jq missing'])
        self.cli.unlink()
        self.assertEqual(self.run_hook(), ['silent:filament CLI missing or not executable'])

    def test_multiple_state_objects_never_consume_wake(self):
        self.assertEqual(self.run_hook(TEST_STATE='{}\n{}'),
                         ['silent:invalid hook state JSON'])

    def test_boot_id_and_uptime_dedupe(self):
        woken = '{"wake_boot_id":"boot-one","down_since":10000,"woke_for":10000}'
        self.assertEqual(self.run_hook(TEST_STATE=woken),
                         ['silent:already woke for this outage'])
        for boot in ('', 'unknown'):
            state, _ = self.wake(self.run_hook(TEST_BOOT=boot))
            self.assertEqual(state['wake_boot_time'], 9900)
            for previous in (9780, 9900, 10020):
                lines = self.run_hook(TEST_BOOT=boot, TEST_STATE=json.dumps(
                    {'wake_boot_time': previous, 'down_since': 10000, 'woke_for': 10000}))
                self.assertEqual(lines, ['silent:already woke for this outage'])
            self.wake(self.run_hook(TEST_BOOT=boot, TEST_STATE='{"wake_boot_time":9000}'))

    def test_front_door_gone_without_a_boot_wakes_once_after_the_grace(self):
        up = {'TEST_UPTIME': '5000', 'TEST_BOOT': 'boot-old',
              'TEST_ENSURE': '{"listener":"alive","role":"backstop"}'}
        lines = self.run_hook(**up)
        self.assertEqual(json.loads(lines[0][6:]), {'down_since': 10000})
        self.assertEqual(lines[1], 'silent:frontdoor gone; waiting 60s')
        self.assertEqual(self.run_hook(TEST_NOW='10030', TEST_STATE='{"down_since":10000}', **up),
                         ['silent:frontdoor gone for 30s'])
        state, payload = self.wake(self.run_hook(
            TEST_NOW='10060', TEST_STATE='{"down_since":10000,"wakes":[1000,7000]}', **up))
        self.assertEqual(payload['event'], 'frontdoor_down')
        self.assertEqual(payload['down_secs'], 60)
        self.assertEqual(payload['wakes_last_hour'], 2)
        self.assertEqual(state, {'down_since': 10000, 'woke_for': 10000, 'wakes': [7000, 10060]})
        self.assertEqual(self.run_hook(TEST_NOW='10500', TEST_STATE=json.dumps(state), **up),
                         ['silent:already woke for this outage'])

    def test_live_or_handling_front_door_ends_the_outage(self):
        state = '{"wake_boot_id":"boot-old","down_since":10000,"woke_for":10000,"wakes":[10060]}'
        for status in ({'listener': 'alive', 'role': 'frontdoor'},
                       {'listener': 'handling', 'role': 'frontdoor'}):
            lines = self.run_hook(TEST_UPTIME='5000', TEST_STATE=state,
                                  TEST_ENSURE=json.dumps(status))
            self.assertEqual(json.loads(lines[0][6:]),
                             {'wake_boot_id': 'boot-old', 'wakes': [10060]})
            self.assertEqual(lines[1], 'silent:frontdoor alive')
        self.assertEqual(self.run_hook(TEST_UPTIME='5000',
                                       TEST_ENSURE='{"listener":"alive","role":"frontdoor"}'),
                         ['silent:frontdoor alive'])

    def test_payload_failure_does_not_record_state(self):
        jq = shutil.which('jq')
        with self.runtime.open('a') as f:
            f.write('\njq() { case "$*" in *wakes_last_hour*) return 1 ;; esac; '
                    + jq + ' "$@"; }\n')
        self.assertEqual(self.run_hook(), ['silent:invalid wake payload'])
