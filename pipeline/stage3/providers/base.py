"""What every Stage 3 provider implements, and helpers they share."""

import re
from typing import Any, Dict, Optional, Protocol


class ProviderFatalError(Exception):
    """An error that affects every call (bad key, unknown model, no credit), so the
    run stops instead of recording the same failure for every prompt."""


class Refactorer(Protocol):
    model: str
    # Request settings beyond the model that change the output (e.g. effort).
    # A saved result is reused only if its settings match.
    settings: Dict[str, Any]

    def run(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        """
        Run one prompt and return its result record. Errors that affect only
        this call are recorded in result['error'] rather than raised.
        """
        ...


def new_result(refactorer: Refactorer) -> Dict[str, Any]:
    return {
        'refactored_code': None,
        'prompt_tokens': None,
        'completion_tokens': None,
        'total_tokens': None,
        'model': refactorer.model,
        'settings': dict(refactorer.settings),
    }


# Matches ```lang\n ... ``` blocks. The language tag is optional and may contain
# characters such as "c++" or "objective-c".
_FENCED_BLOCK_RE = re.compile(r"```[^\n`]*\n(.*?)\n?```", re.DOTALL)
# Matches an opening fence with no closing fence (a response cut off at the
# token limit).
_UNCLOSED_BLOCK_RE = re.compile(r"```[^\n`]*\n(.*)\Z", re.DOTALL)


def extract_code(response_text: Optional[str]) -> str:
    """
    Return only the raw code from an LLM response.

    - One or more fenced blocks: the longest block is returned, since a short
      extra block is usually an aside (e.g. a one-line import note).
    - An opening fence with no closing fence: everything after the fence.
    - No fences: the whole response, assumed to be bare code.
    """
    if not response_text:
        return ''

    blocks = _FENCED_BLOCK_RE.findall(response_text)
    if blocks:
        return max(blocks, key=len).rstrip()

    unclosed = _UNCLOSED_BLOCK_RE.search(response_text)
    if unclosed:
        return unclosed.group(1).rstrip()

    return response_text.strip('\n').rstrip()
