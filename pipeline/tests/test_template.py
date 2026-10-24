"""The prompts for one issue must differ only in their context section."""

import unittest

from pipeline.stage2.models import ContextChunk, IssueRecord, RuleInfo
from pipeline.stage2.template import CODE_HEADING, CONTEXT_HEADING, render_prompt

ISSUE = IssueRecord(
    issue_key='K1', rule='dart:S3358', message='Extract this nested ternary operation.',
    file_path='lib/a.dart', start_line=11, end_line=11, snippet_start_line=10,
    snippet_end_line=12, target_snippet="x = a\n  ? b\n  : (c ? d : e);", scope_kind='expression')
RULE = RuleInfo(key='dart:S3358', name='Ternary operators should not be nested',
                description='Nested ternaries are hard to read.', description_source='curated')
CHUNK = ContextChunk(text='```dart\n// lib/b.dart\nconst d = 1;\n```', method='vector',
                     source_path='lib/b.dart', start_line=1, end_line=1)


def split(prompt: str):
    head, rest = prompt.split(CONTEXT_HEADING, 1)
    context, tail = rest.split(CODE_HEADING, 1)
    return head, context, tail


class TemplateTest(unittest.TestCase):
    def setUp(self):
        self.prompts = {
            'zero_shot': render_prompt(ISSUE, RULE, 'zero_shot', []),
            'few_shot': render_prompt(ISSUE, RULE, 'few_shot', [CHUNK]),
            'repository_aware': render_prompt(ISSUE, RULE, 'repository_aware', [CHUNK]),
        }

    def test_only_context_differs(self):
        parts = {name: split(text) for name, text in self.prompts.items()}
        heads = {p[0] for p in parts.values()}
        tails = {p[2] for p in parts.values()}
        self.assertEqual(len(heads), 1, "text before the context section differs")
        self.assertEqual(len(tails), 1, "text after the context section differs")
        self.assertEqual(len({p[1] for p in parts.values()}), 3)

    def test_contains_rule_description_and_snippet(self):
        for text in self.prompts.values():
            self.assertIn(RULE.description, text)
            self.assertIn(ISSUE.target_snippet, text)
            self.assertIn('Flagged lines: 11 (the code below covers lines 10-12)', text)

    def test_context_chunks_are_included(self):
        self.assertIn(CHUNK.text, self.prompts['repository_aware'])
        self.assertNotIn(CHUNK.text, self.prompts['zero_shot'])

    def test_flagged_code(self):
        self.assertEqual(ISSUE.flagged_code, '  ? b')


if __name__ == '__main__':
    unittest.main()
