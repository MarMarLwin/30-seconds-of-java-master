"""Stage 3 logic that needs no API calls: providers are given fake clients."""

import unittest
from types import SimpleNamespace

from pipeline.stage3.providers import create_refactorer
from pipeline.stage3.providers.base import extract_code
from pipeline.stage3.refactor import is_reusable

try:
    from pipeline.stage3.providers.claude import FALLBACK_BETA, ClaudeRefactorer
except Exception:  # anthropic not installed
    ClaudeRefactorer = None


def fake_response(text='```dart\nfinal x = 1;\n```', stop_reason='end_turn',
                  model='claude-opus-5-5', iterations=None):
    usage = SimpleNamespace(
        input_tokens=100, output_tokens=40, cache_creation_input_tokens=0,
        cache_read_input_tokens=20, iterations=iterations,
        to_dict=lambda: {'input_tokens': 100, 'output_tokens': 40})
    return SimpleNamespace(
        content=[SimpleNamespace(type='thinking', thinking=''),
                 SimpleNamespace(type='text', text=text)],
        stop_reason=stop_reason, model=model, usage=usage,
        stop_details=SimpleNamespace(category='cyber') if stop_reason == 'refusal' else None)


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


@unittest.skipIf(ClaudeRefactorer is None, 'anthropic is not installed')
class ClaudeProviderTest(unittest.TestCase):
    def make(self, response, model=None, effort=None):
        refactorer = ClaudeRefactorer(model, effort)
        messages = FakeMessages(response)
        refactorer.client = SimpleNamespace(messages=messages,
                                            beta=SimpleNamespace(messages=messages))
        return refactorer, messages

    def test_request_uses_effort_and_fallbacks_without_temperature(self):
        refactorer, messages = self.make(fake_response())
        refactorer.run('system', 'user')
        call = messages.calls[0]
        self.assertEqual(call['model'], 'claude-opus-5-5')
        self.assertEqual(call['output_config'], {'effort': 'high'})
        self.assertEqual(call['fallbacks'], 'default')
        self.assertEqual(call['betas'], [FALLBACK_BETA])
        self.assertEqual(call['system'], 'system')
        self.assertNotIn('temperature', call)
        self.assertNotIn('thinking', call)

    def test_no_fallbacks_for_models_without_them(self):
        refactorer, messages = self.make(fake_response(model='claude-haiku-5-5'),
                                         model='claude-haiku-5-5', effort='low')
        refactorer.run('system', 'user')
        self.assertNotIn('fallbacks', messages.calls[0])
        self.assertEqual(refactorer.settings, {'effort': 'low', 'fallbacks': None})

    def test_result_fields(self):
        refactorer, _ = self.make(fake_response())
        result = refactorer.run('system', 'user')
        self.assertEqual(result['refactored_code'], 'final x = 1;')
        self.assertEqual(result['prompt_tokens'], 120)        # uncached + cache read
        self.assertEqual(result['completion_tokens'], 40)
        self.assertEqual(result['total_tokens'], 160)
        self.assertEqual(result['served_by'], 'claude-opus-5-5')
        self.assertFalse(result['fallback_used'])
        self.assertEqual(result['settings'], {'effort': 'high', 'fallbacks': 'default'})
        self.assertNotIn('error', result)

    def test_fallback_is_recorded(self):
        iterations = [SimpleNamespace(type='message'), SimpleNamespace(type='fallback_message')]
        refactorer, _ = self.make(fake_response(model='claude-opus-4-8', iterations=iterations))
        result = refactorer.run('system', 'user')
        self.assertTrue(result['fallback_used'])
        self.assertEqual(result['served_by'], 'claude-opus-4-8')

    def test_refusal_and_truncation_are_errors(self):
        refactorer, _ = self.make(fake_response(text='', stop_reason='refusal'))
        self.assertIn('declined', refactorer.run('s', 'u')['error'])
        refactorer, _ = self.make(fake_response(stop_reason='max_tokens'))
        self.assertIn('cut off', refactorer.run('s', 'u')['error'])

    def test_factory(self):
        self.assertIsInstance(create_refactorer('claude', effort='max'), ClaudeRefactorer)


CLAUDE_CODE_OUTPUT = {
    'type': 'result', 'subtype': 'success', 'is_error': False, 'stop_reason': 'end_turn',
    'result': '```dart\nfinal y = 2;\n```', 'total_cost_usd': 0.0123, 'duration_ms': 2000,
    'usage': {'input_tokens': 300, 'output_tokens': 50, 'cache_creation_input_tokens': 0,
              'cache_read_input_tokens': 10},
    'modelUsage': {
        'claude-haiku-4-5-20251001': {'inputTokens': 500, 'outputTokens': 11},
        'claude-sonnet-5': {'inputTokens': 300, 'outputTokens': 50},
    },
}


class ClaudeCodeProviderTest(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        from pipeline.stage3.providers import claude_code
        self.module = claude_code
        patcher = mock.patch.object(claude_code.shutil, 'which', return_value='/bin/claude')
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'sk-x', 'KEEP': '1'})
        env.start()
        self.addCleanup(env.stop)

    def run_with(self, stdout, model=None, returncode=0):
        from unittest import mock
        refactorer = self.module.ClaudeCodeRefactorer(model, None)
        done = SimpleNamespace(stdout=stdout, stderr='', returncode=returncode)
        with mock.patch.object(self.module.subprocess, 'run', return_value=done) as run:
            result = refactorer.run('SYSTEM', 'USER')
        return refactorer, result, run

    def test_command_and_environment(self):
        import json
        refactorer, _, run = self.run_with(json.dumps(CLAUDE_CODE_OUTPUT), model='opus')
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:4], ['/bin/claude', '-p', '--output-format', 'json'])
        for flag in ('--safe-mode', '--strict-mcp-config', '--no-session-persistence'):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index('--tools') + 1], '')
        self.assertEqual(cmd[cmd.index('--system-prompt') + 1], 'SYSTEM')
        self.assertEqual(cmd[cmd.index('--model') + 1], 'opus')
        self.assertEqual(run.call_args.kwargs['input'], 'USER')
        env = run.call_args.kwargs['env']
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertEqual(env['KEEP'], '1')
        self.assertEqual(refactorer.model, 'opus')

    def test_result_fields(self):
        import json
        refactorer, result, run = self.run_with(json.dumps(CLAUDE_CODE_OUTPUT))
        self.assertNotIn('--model', run.call_args.args[0])
        self.assertEqual(refactorer.model, 'claude-code')
        self.assertEqual(result['refactored_code'], 'final y = 2;')
        self.assertEqual(result['served_by'], 'claude-sonnet-5')
        self.assertEqual(result['prompt_tokens'], 310)
        self.assertEqual(result['completion_tokens'], 50)
        self.assertEqual(result['equivalent_api_cost_usd'], 0.0123)
        self.assertEqual(result['settings'], {'via': 'claude-code', 'effort': 'high'})
        self.assertNotIn('error', result)

    def test_usage_limit_stops_the_run(self):
        import json
        from pipeline.stage3.providers import ProviderFatalError
        output = {'is_error': True, 'result': 'Claude AI usage limit reached|1760000000'}
        with self.assertRaises(ProviderFatalError):
            self.run_with(json.dumps(output), returncode=1)

    def test_other_errors_are_recorded(self):
        import json
        _, result, _ = self.run_with(json.dumps({'is_error': True, 'result': 'overloaded'}))
        self.assertIn('overloaded', result['error'])
        _, result, _ = self.run_with('not json', returncode=2)
        self.assertIn('exited with 2', result['error'])


class ReuseTest(unittest.TestCase):
    def test_settings_must_match(self):
        refactorer = SimpleNamespace(model='m', settings={'effort': 'high'})
        result = {'model': 'm', 'settings': {'effort': 'high'}, 'prompt_sha256': 'abc'}
        self.assertTrue(is_reusable(result, refactorer, 'abc'))
        self.assertFalse(is_reusable({**result, 'settings': {'effort': 'low'}},
                                     refactorer, 'abc'))
        self.assertFalse(is_reusable(result, refactorer, 'other'))
        self.assertFalse(is_reusable({**result, 'error': 'x'}, refactorer, 'abc'))


class ExtractCodeTest(unittest.TestCase):
    def test_longest_block_and_unclosed_fence(self):
        self.assertEqual(extract_code('```\na\n```\ntext\n```dart\nlonger\n```'), 'longer')
        self.assertEqual(extract_code('```dart\ncut off'), 'cut off')
        self.assertEqual(extract_code('plain'), 'plain')
        self.assertEqual(extract_code(None), '')


if __name__ == '__main__':
    unittest.main()
