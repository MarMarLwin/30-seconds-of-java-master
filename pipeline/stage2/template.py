"""
The single prompt template shared by every strategy.

Everything except the "## Context" section is identical across strategies
for a given issue (tests/test_template.py checks this), so the strategy
comparison varies one thing only. Output-format instructions live in Stage 3's
system prompt, which is also shared.
"""

from typing import List, Optional

from .models import ContextChunk, IssueRecord, RuleInfo, fence_language

CONTEXT_HEADING = '## Context'
CODE_HEADING = '## Code to refactor'

PROMPT_TEMPLATE = """\
Refactor the code below to resolve the SonarQube issue described here.

## Issue
Rule: {rule_key} ({rule_name})
Message: {message}
File: {file_path}
Flagged lines: {flagged_lines} (the code below covers lines {snippet_lines})

## Rule description
{rule_description}

{context_heading}
{context}

{code_heading}
```{fence}
{snippet}
```
"""

# The first line of each strategy's context section.
CONTEXT_INTROS = {
    'zero_shot': 'No additional context is provided.',
    'few_shot': 'Examples of the same kind of refactoring, from other code:',
    'repository_aware': ('Related code from this repository, for reference only. '
                         'Do not return or modify it:'),
}


def _range(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"


def render_context(strategy: str, chunks: List[ContextChunk]) -> str:
    intro = CONTEXT_INTROS[strategy]
    if not chunks:
        if strategy == 'zero_shot':
            return intro
        return f"{intro}\n\n(Nothing relevant was found.)"
    return intro + '\n\n' + '\n\n'.join(chunk.text for chunk in chunks)


def render_prompt(issue: IssueRecord, rule: RuleInfo, strategy: str,
                  chunks: Optional[List[ContextChunk]] = None) -> str:
    return PROMPT_TEMPLATE.format(
        rule_key=issue.rule,
        rule_name=rule.name,
        message=issue.message,
        file_path=issue.file_path,
        flagged_lines=_range(issue.start_line, issue.end_line),
        snippet_lines=_range(issue.snippet_start_line, issue.snippet_end_line),
        rule_description=rule.description,
        context_heading=CONTEXT_HEADING,
        context=render_context(strategy, chunks or []),
        code_heading=CODE_HEADING,
        fence=fence_language(issue.file_path),
        snippet=issue.target_snippet,
    )
