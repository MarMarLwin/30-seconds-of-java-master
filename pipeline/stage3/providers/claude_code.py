"""
Claude through the installed Claude Code CLI (`claude -p`), billed to the
Claude subscription it is logged in with (e.g. a Pro plan) instead of API
credits.

Each prompt runs as a fresh headless session that is kept as close to a plain
model call as Claude Code allows:
  - Stage 3's system prompt replaces Claude Code's (--system-prompt),
  - no tools (--tools ""), so the model can only answer,
  - --safe-mode: no CLAUDE.md, skills, plugins, hooks or MCP servers,
  - an empty temporary working directory, and no saved session.

ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN are removed from the CLI's
environment, because Claude Code bills to an API key whenever one is set.
The subscription's usage limits apply; when they are reached the run stops
and can be resumed later.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, Optional

from .base import ProviderFatalError, extract_code, new_result

logger = logging.getLogger(__name__)

# low | medium | high | xhigh | max, as for --provider claude.
DEFAULT_EFFORT = 'high'
EFFORT_LEVELS = ('low', 'medium', 'high', 'xhigh', 'max')

# Label used for the results file name when no --model is given. Claude Code
# then uses the plan's default model; each result records the model that ran.
DEFAULT_MODEL_LABEL = 'claude-code'

CALL_TIMEOUT_SECONDS = 600

# Removed from the CLI's environment so it uses the subscription login.
API_CREDENTIAL_VARS = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')

_LIMIT_MARKERS = ('usage limit', 'limit reached', 'rate limit', 'out of extra usage')
_LOGIN_MARKERS = ('not logged in', 'please run /login', 'invalid api key',
                  'authentication', 'oauth')


class ClaudeCodeRefactorer:
    def __init__(self, model: Optional[str], effort: Optional[str]):
        self.executable = shutil.which('claude')
        if not self.executable:
            raise ProviderFatalError("The claude CLI was not found on PATH. Install Claude "
                                     "Code and log in with `claude` first.")
        self.requested_model = model
        self.model = model or DEFAULT_MODEL_LABEL
        self.effort = effort or DEFAULT_EFFORT
        if self.effort not in EFFORT_LEVELS:
            raise ProviderFatalError(f"Unknown effort {self.effort!r}; use one of "
                                     f"{', '.join(EFFORT_LEVELS)}")
        self.settings: Dict[str, Any] = {'via': 'claude-code', 'effort': self.effort}
        self.env = {k: v for k, v in os.environ.items() if k not in API_CREDENTIAL_VARS}

    def _command(self, system_prompt: str) -> list:
        cmd = [self.executable, '-p', '--output-format', 'json',
               '--system-prompt', system_prompt,
               '--tools', '', '--safe-mode', '--strict-mcp-config',
               '--no-session-persistence', '--effort', self.effort]
        if self.requested_model:
            cmd += ['--model', self.requested_model]
        return cmd

    def run(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        result = new_result(self)
        with tempfile.TemporaryDirectory(prefix='stage3-claude-code-') as workdir:
            try:
                done = subprocess.run(self._command(system_prompt), input=user_prompt,
                                      capture_output=True, text=True, cwd=workdir,
                                      env=self.env, timeout=CALL_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                result['error'] = f"claude -p timed out after {CALL_TIMEOUT_SECONDS}s"
                return result

        try:
            data = json.loads(done.stdout)
        except json.JSONDecodeError:
            message = (done.stderr or done.stdout).strip()[-500:]
            self._raise_if_fatal(message)
            result['error'] = f"claude -p exited with {done.returncode}: {message}"
            return result

        if data.get('is_error'):
            message = str(data.get('result') or data.get('subtype') or 'unknown error')
            self._raise_if_fatal(message)
            result['error'] = f"Claude Code error: {message[:500]}"
            return result

        self._fill(result, data)
        return result

    @staticmethod
    def _raise_if_fatal(message: str) -> None:
        lowered = message.lower()
        if any(marker in lowered for marker in _LIMIT_MARKERS):
            raise ProviderFatalError(f"Claude plan usage limit reached; re-run later to "
                                     f"resume where it stopped. ({message[:300]})")
        if any(marker in lowered for marker in _LOGIN_MARKERS):
            raise ProviderFatalError(f"Claude Code is not logged in to a subscription. Run "
                                     f"`claude` once and log in. ({message[:300]})")

    def _fill(self, result: Dict[str, Any], data: Dict[str, Any]) -> None:
        text = data.get('result') or ''
        usage = data.get('usage') or {}
        model_usage = data.get('modelUsage') or {}

        result['refactored_code'] = extract_code(text)
        result['finish_reason'] = data.get('stop_reason')
        result['served_by'] = _main_model(model_usage, usage) or self.requested_model

        # Token counts are for the model that answered. Claude Code may also make
        # small side calls (e.g. on Haiku); those are kept in model_usage.
        prompt = sum(usage.get(k) or 0 for k in ('input_tokens', 'cache_creation_input_tokens',
                                                  'cache_read_input_tokens'))
        output = usage.get('output_tokens') or 0
        result['prompt_tokens'] = prompt
        result['completion_tokens'] = output
        result['total_tokens'] = prompt + output
        result['usage'] = usage
        result['model_usage'] = model_usage
        # What the call would cost on the API; not charged on a subscription.
        result['equivalent_api_cost_usd'] = data.get('total_cost_usd')
        result['duration_ms'] = data.get('duration_ms')

        if data.get('stop_reason') == 'refusal':
            result['error'] = 'Claude declined the request'
        elif data.get('stop_reason') == 'max_tokens':
            result['error'] = 'Response was cut off at the output token limit'
        elif not result['refactored_code']:
            result['error'] = 'Response contained no code'


def _main_model(model_usage: Dict[str, Any], usage: Dict[str, Any]) -> Optional[str]:
    """The model whose token counts match the top-level usage (the answering model)."""
    for name, counts in model_usage.items():
        if (counts.get('inputTokens') == usage.get('input_tokens')
                and counts.get('outputTokens') == usage.get('output_tokens')):
            return name
    return next(iter(model_usage), None)
