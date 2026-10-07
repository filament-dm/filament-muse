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
date() { printf '10000'; }
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

    def test_backstop_alive_or_handling_still_wakes(self):
        for status in ({'listener': 'none'}, {'listener': 'alive', 'role': 'backstop'},
                       {'listener': 'handling', 'role': 'backstop'},
                       {'listener': 'handling', 'role': 'frontdoor'},
                       {'listener': 'alive'}, {'listener': 'paused'},
                       {'listener': 'handling'}, {'listener': 'unknown'}):
            lines = self.run_hook(TEST_ENSURE=json.dumps(status))
            self.assertEqual(len(lines), 2)
            self.assertTrue(lines[0].startswith('state:'))
            self.assertEqual(json.loads(lines[1][5:])['ensure'], status)
        self.assertTrue(self.run_hook(TEST_ENSURE=json.dumps(
            {'listener': 'alive', 'role': 'frontdoor'}))[0].startswith('silent:'))

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

    def test_boot_id_and_uptime_dedupe(self):
        self.assertTrue(self.run_hook(TEST_STATE='{"wake_boot_id":"boot-one"}')[0].startswith('silent:'))
        self.assertTrue(self.run_hook(TEST_UPTIME='900')[0].startswith('silent:'))
        for boot in ('', 'unknown'):
            lines = self.run_hook(TEST_BOOT=boot)
            state = json.loads(lines[0][6:])
            self.assertEqual(state, {'wake_boot_time': 9900})
            for previous in (9780, 9900, 10020):
                lines = self.run_hook(TEST_BOOT=boot, TEST_STATE=json.dumps({'wake_boot_time': previous}))
                self.assertTrue(lines[0].startswith('silent:'))
            lines = self.run_hook(TEST_BOOT=boot, TEST_STATE='{"wake_boot_time":9000}')
            self.assertTrue(lines[0].startswith('state:'))

    def test_payload_failure_does_not_record_state(self):
        jq = shutil.which('jq')
        with self.runtime.open('a') as f:
            f.write('\njq() { case "$*" in *vm_replaced*) return 1 ;; esac; '
                    + jq + ' "$@"; }\n')
        self.assertEqual(self.run_hook(), ['silent:invalid wake payload'])
