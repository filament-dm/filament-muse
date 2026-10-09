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

    @staticmethod
    def line(kind, value):
        return kind + ':' + json.dumps(value, separators=(',', ':'))

    def grace(self, state=None, now=10000):
        return [self.line('state', dict(state or {}, down_since=now)),
                'silent:frontdoor down; grace started']

    def health_wake(self, state, now=10000, uptime=900, ensure=None, notify=True):
        return [self.line('state', dict(state, last_health_wake=now,
                                       notified_down_since=state['down_since'])),
                self.line('wake', dict(event='listener_down', down_since=state['down_since'],
                                      uptime_secs=uptime, ensure=ensure or {'listener': 'none'},
                                      notify=notify))]

    def test_paused_and_healthy_clear_outage(self):
        boot = {'wake_boot_id': 'boot-one', 'wake_boot_time': 9900}
        for status, reason in (({'listener': 'paused'}, 'auth paused; not an outage'),
                               ({'listener': 'alive', 'role': 'frontdoor'}, 'frontdoor healthy'),
                               ({'listener': 'handling', 'role': 'frontdoor'}, 'frontdoor healthy')):
            for health in ({}, {'down_since': 9800, 'last_health_wake': 9900,
                               'notified_down_since': 9800}, {'down_since': None}):
                with self.subTest(status=status, health=health):
                    expected = ([self.line('state', boot)] if health else []) + ['silent:' + reason]
                    self.assertEqual(self.run_hook(TEST_STATE=json.dumps(dict(boot, **health)),
                                                   TEST_ENSURE=json.dumps(status)), expected)

    def test_down_statuses_start_grace_on_old_vm_and_wake_on_new_boot(self):
        # handling is a CLI status; unknown exercises a defensive fallback.
        for status in ({'listener': 'none'}, {'listener': 'alive', 'role': 'backstop'},
                       {'listener': 'handling', 'role': 'backstop'}, {'listener': 'alive'},
                       {'listener': 'handling'}, {'listener': 'unknown'}, {}):
            with self.subTest(status=status):
                self.assertEqual(self.run_hook(TEST_UPTIME='900', TEST_ENSURE=json.dumps(status)),
                                 self.grace())
                self.assertEqual(self.run_hook(TEST_ENSURE=json.dumps(status)),
                                 [self.line('state', {'wake_boot_id': 'boot-one'}),
                                  self.line('wake', dict(event='vm_replaced', boot_id='boot-one',
                                                        uptime_secs=60, ensure=status))])

    def test_grace_and_cooldown_boundaries(self):
        for age in (30, 89):
            self.assertEqual(self.run_hook(TEST_UPTIME='900',
                                           TEST_STATE=json.dumps({'down_since': 10000 - age})),
                             [f'silent:frontdoor down {age}s; within grace'])
        state = {'wake_boot_id': 'boot-one', 'down_since': 9910}
        ensure = {'listener': 'handling', 'role': 'backstop'}
        self.assertEqual(self.run_hook(TEST_UPTIME='900', TEST_STATE=json.dumps(state),
                                       TEST_ENSURE=json.dumps(ensure)),
                         self.health_wake(state, ensure=ensure))
        for age in (100, 599):
            state = {'down_since': 9800, 'last_health_wake': 10000 - age}
            self.assertEqual(self.run_hook(TEST_UPTIME='900', TEST_STATE=json.dumps(state)),
                             [f'silent:woke {age}s ago for this outage; cooldown'])
        state = {'down_since': 9800, 'last_health_wake': 9400, 'notified_down_since': 9800}
        self.assertEqual(self.run_hook(TEST_UPTIME='900', TEST_STATE=json.dumps(state)),
                         self.health_wake(state, notify=False))
        state['notified_down_since'] = 9700
        self.assertEqual(self.run_hook(TEST_UPTIME='900', TEST_STATE=json.dumps(state)),
                         self.health_wake(state))

    def test_recovery_and_pause_start_fresh_outages(self):
        for recovery in ({'listener': 'alive', 'role': 'frontdoor'}, {'listener': 'paused'}):
            state = {'wake_boot_id': 'boot-one'}

            def poll(now, ensure, expected):
                nonlocal state
                lines = self.run_hook(TEST_UPTIME='1000', TEST_NOW=str(now),
                                      TEST_STATE=json.dumps(state), TEST_ENSURE=json.dumps(ensure))
                self.assertEqual(lines, expected)
                if lines[0].startswith('state:'):
                    state = json.loads(lines[0][6:])

            poll(10000, {'listener': 'none'}, self.grace(state))
            poll(10090, {'listener': 'none'}, self.health_wake(state, now=10090, uptime=1000))
            reason = 'auth paused; not an outage' if recovery['listener'] == 'paused' else 'frontdoor healthy'
            poll(10100, recovery, [self.line('state', {'wake_boot_id': 'boot-one'}), 'silent:' + reason])
            poll(10110, {'listener': 'none'}, self.grace(state, now=10110))
            poll(10200, {'listener': 'none'}, self.health_wake(state, now=10200, uptime=1000))

    def test_boot_wake_clears_health_then_retries_without_second_boot_wake(self):
        state = {'wake_boot_id': 'old-boot', 'down_since': 9000,
                 'last_health_wake': 9990, 'notified_down_since': 9000}
        lines = self.run_hook(TEST_STATE=json.dumps(state))
        self.assertEqual(lines, [self.line('state', {'wake_boot_id': 'boot-one'}),
                                 self.line('wake', dict(event='vm_replaced', boot_id='boot-one',
                                                       uptime_secs=60, ensure={'listener': 'none'}))])
        lines = self.run_hook(TEST_STATE=lines[0][6:])
        self.assertEqual(lines, self.grace({'wake_boot_id': 'boot-one'}))
        state = json.loads(lines[0][6:])
        self.assertEqual(self.run_hook(TEST_STATE=json.dumps(state), TEST_NOW='10090', TEST_UPTIME='150'),
                         self.health_wake(state, now=10090, uptime=150))

    def test_approximate_boot_dedupe(self):
        for boot in ('', 'unknown'):
            expected = [self.line('state', {'wake_boot_time': 9900}),
                        self.line('wake', dict(event='vm_replaced', boot_id=boot, uptime_secs=60,
                                              ensure={'listener': 'none'}))]
            self.assertEqual(self.run_hook(TEST_BOOT=boot), expected)
            for previous in (9780, 9900, 10020):
                state = {'wake_boot_time': previous}
                self.assertEqual(self.run_hook(TEST_BOOT=boot, TEST_STATE=json.dumps(state)), self.grace(state))
            for previous in (9779, 10021, 9000):
                self.assertEqual(self.run_hook(TEST_BOOT=boot,
                                               TEST_STATE=json.dumps({'wake_boot_time': previous})), expected)

    def test_malformed_health_fields_are_absent(self):
        for key in ('down_since', 'last_health_wake', 'notified_down_since'):
            for value in ('oops', None, 1.5, 20000, -1, True, [], {}):
                with self.subTest(key=key, value=value):
                    state = {'wake_boot_id': 'boot-one', key: value}
                    self.assertEqual(self.run_hook(TEST_UPTIME='900', TEST_STATE=json.dumps(state)),
                                     self.grace({'wake_boot_id': 'boot-one'}))
                    if key != 'down_since':
                        state['down_since'] = 9910
                        self.assertEqual(self.run_hook(TEST_UPTIME='900', TEST_STATE=json.dumps(state)),
                                         self.health_wake({'wake_boot_id': 'boot-one', 'down_since': 9910}))
        for future in (10001, 10300):
            self.assertEqual(self.run_hook(TEST_UPTIME='900', TEST_STATE=json.dumps({'down_since': future})),
                             self.grace())
        # Zero is present, and the allowed future skew applies to cooldown keys.
        self.assertEqual(self.run_hook(TEST_UPTIME='900', TEST_STATE='{"down_since":0}'),
                         self.health_wake({'down_since': 0}))
        self.assertEqual(self.run_hook(TEST_UPTIME='900',
                                       TEST_STATE='{"down_since":9910,"last_health_wake":10300}'),
                         ['silent:woke -300s ago for this outage; cooldown'])

    def test_invalid_ensure_or_state_never_consumes_wake(self):
        for uptime in ('60', '900'):
            for key, values, reason in (
                    ('TEST_ENSURE', ('broken', '{}\n{}', '[]', 'null'), 'invalid ensure JSON'),
                    ('TEST_STATE', ('broken', '{}\n{}', '[]', 'null'), 'invalid hook state JSON'),
                    ('TEST_EXIT', ('1',), 'filament ensure unavailable')):
                for value in values:
                    self.assertEqual(self.run_hook(TEST_UPTIME=uptime, **{key: value}), ['silent:' + reason])
            self.assertEqual(self.run_hook(TEST_UPTIME=uptime, TEST_ENSURE='broken',
                                           TEST_STATE='{"down_since":9910}'), ['silent:invalid ensure JSON'])

    def test_missing_dependencies_are_silent(self):
        self.assertEqual(self.run_hook(PATH=str(self.root)), ['silent:jq missing'])
        self.cli.unlink()
        self.assertEqual(self.run_hook(), ['silent:filament CLI missing or not executable'])

    def test_unavailable_preflights_are_silent(self):
        self.assertEqual(self.run_hook(TEST_UPTIME='broken'), ['silent:uptime unavailable'])
        self.assertEqual(self.run_hook(TEST_NOW='broken'), ['silent:clock unavailable'])
        with self.runtime.open('a') as f:
            f.write('\nhook_state_get() { return 1; }\n')
        self.assertEqual(self.run_hook(), ['silent:hook state unavailable'])

    def test_payload_failure_does_not_record_state(self):
        jq = shutil.which('jq')
        for event, state, uptime in (('vm_replaced', {}, '60'),
                                     ('listener_down', {'down_since': 9910}, '900')):
            # Match the payload filter instead of an N-th call so harmless jq
            # refactoring cannot move the injected failure to a different step.
            original = self.runtime.read_text()
            for failure in ('return 1', "printf '[]'; return 0"):
                with self.subTest(event=event, failure=failure):
                    self.runtime.write_text(original + '\njq() { case "$*" in *' + event + '*) '
                                            + failure + ' ;; esac; "' + jq + '" "$@"; }\n')
                    self.assertEqual(self.run_hook(TEST_UPTIME=uptime, TEST_STATE=json.dumps(state)),
                                     ['silent:invalid wake payload'])
            self.runtime.write_text(original)

    def test_wake_text(self):
        with self.runtime.open('a') as f:
            f.write('\nwake() { printf "wake-text:%s\\n" "$1"; }\n')
        self.assertEqual(self.run_hook(), [self.line('state', {'wake_boot_id': 'boot-one'}),
                         'wake-text:VM was replaced (uptime 60s < 15min) and no live frontdoor is present'])
        self.assertEqual(self.run_hook(TEST_UPTIME='900', TEST_STATE='{"down_since":9910}'),
                         [self.line('state', {'down_since': 9910, 'last_health_wake': 10000,
                                              'notified_down_since': 9910}),
                          'wake-text:Filament frontdoor down since 9910 (90s) and not healed'])
