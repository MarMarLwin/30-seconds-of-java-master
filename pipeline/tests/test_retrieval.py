import unittest

from pipeline.stage2.config import FEW_SHOT_EXAMPLES_FILE
from pipeline.stage2.models import ContextChunk
from pipeline.common.dart_ast import Declaration, FileOutline
from pipeline.stage2.retrieval.import_graph import resolve_uri
from pipeline.stage2.strategies.few_shot import load_examples
from pipeline.stage2.strategies.repo_aware import _duplicates, _matching, references_in


class ImportResolutionTest(unittest.TestCase):
    def test_package_uri(self):
        self.assertEqual(resolve_uri('package:app/utils/x.dart', 'lib/a/b.dart', 'app'),
                         'lib/utils/x.dart')

    def test_relative_uri(self):
        self.assertEqual(resolve_uri('../../utils/x.dart', 'lib/features/a/b.dart', 'app'),
                         'lib/utils/x.dart')

    def test_external_uris(self):
        for uri in ('dart:async', 'package:flutter/material.dart', 'http://x/y.dart'):
            self.assertIsNone(resolve_uri(uri, 'lib/a.dart', 'app'), uri)

    def test_escaping_repo(self):
        self.assertIsNone(resolve_uri('../../x.dart', 'lib/a.dart', 'app'))


class ReferenceTest(unittest.TestCase):
    def test_receivers(self):
        refs = references_in("final s = await authNotifier.logout();\n"
                             "context.go(AppRoute.login.name);\n"
                             "foo().bar; // comment.ignored\n"
                             "print('AppLabels.inString');")
        self.assertIn(('authNotifier', None), refs)
        self.assertIn(('logout', 'authNotifier'), refs)
        self.assertIn(('AppRoute', None), refs)
        self.assertIn(('login', 'AppRoute'), refs)
        self.assertIn(('bar', ''), refs)
        names = {name for name, _ in refs}
        self.assertNotIn('ignored', names)
        self.assertNotIn('inString', names)
        self.assertNotIn('name', names)       # ignored as too generic

    def test_matching(self):
        outline = FileOutline('lib/a.dart')
        top = Declaration('variable', 'email', None, 1, 1, 'final email = 1;')
        labels_field = Declaration('field', 'email', 'AppLabels', 5, 5, "static const email = '';")
        other_field = Declaration('field', 'email', 'User', 9, 9, 'final String email;')
        candidates = [(outline, top), (outline, labels_field), (outline, other_field)]

        self.assertEqual([d for _, d in _matching(candidates, None)], [top])
        self.assertEqual([d for _, d in _matching(candidates, 'AppLabels')], [labels_field])
        self.assertEqual([d for _, d in _matching(candidates, 'user')],
                         [labels_field, other_field])


class DuplicateTest(unittest.TestCase):
    def chunk(self, start, end, summary=False):
        return ContextChunk('x', 'imported_declaration', 'lib/a.dart', start, end,
                            summary=summary)

    def test_overlapping_code_is_duplicate(self):
        self.assertTrue(_duplicates(self.chunk(5, 10), self.chunk(8, 12)))

    def test_member_inside_outline_is_not_duplicate(self):
        self.assertFalse(_duplicates(self.chunk(5, 10), self.chunk(1, 20, summary=True)))

    def test_one_line_member_inside_outline_is_duplicate(self):
        self.assertTrue(_duplicates(self.chunk(5, 5), self.chunk(1, 20, summary=True)))


class FewShotDataTest(unittest.TestCase):
    def test_examples_are_well_formed(self):
        examples = load_examples(FEW_SHOT_EXAMPLES_FILE)
        self.assertTrue(examples)
        for rule, items in examples.items():
            for item in items:
                self.assertNotEqual(item['before'].strip(), item['after'].strip(), rule)


if __name__ == '__main__':
    unittest.main()
