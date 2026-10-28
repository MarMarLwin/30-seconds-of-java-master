import tempfile
import unittest
from pathlib import Path

from pipeline.stage2.config import FEW_SHOT_EXAMPLES_FILE, RULE_DESCRIPTIONS_FILE
from pipeline.stage2.models import ContextChunk
from pipeline.common.ast_outline import Declaration, FileOutline
from pipeline.stage2.retrieval.import_graph import (JavaImportResolver, java_source_root,
                                                    resolve_uri)
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

    def test_java_keywords_and_block_comments(self):
        refs = references_in("/* Order.total */ int total = Pricing.withTax(order);\n"
                             "System.out.println(total);", 'java')
        self.assertIn(('Pricing', None), refs)
        self.assertIn(('withTax', 'Pricing'), refs)
        self.assertIn(('order', None), refs)
        names = {name for name, _ in refs}
        self.assertNotIn('Order', names)        # inside a block comment
        self.assertNotIn('int', names)
        self.assertNotIn('System', names)
        self.assertNotIn('println', names)

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


class JavaImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for path in ('src/main/java/shop/Cart.java', 'src/main/java/shop/Item.java',
                     'src/main/java/shop/util/Money.java', 'src/main/java/shop/util/Tax.java',
                     'src/main/java/other/Rates.java', 'src/test/java/shop/CartTest.java'):
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text('')
        self.resolver = JavaImportResolver(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_root(self):
        self.assertEqual(java_source_root('src/main/java/a/b/C.java', 'a.b'), 'src/main/java')
        self.assertEqual(java_source_root('a/b/C.java', 'a.b'), '')
        self.assertEqual(java_source_root('src/C.java', ''), 'src')
        self.assertIsNone(java_source_root('src/x/C.java', 'a.b'))

    def test_imports_then_own_package_across_roots(self):
        outline = FileOutline('src/test/java/shop/CartTest.java', imports=[
            {'uri': 'shop', 'kind': 'package'},
            {'uri': 'shop.util.*', 'kind': 'import'},
            {'uri': 'other.Rates', 'kind': 'import'},
            {'uri': 'other.Rates.Inner', 'kind': 'import'},
            {'uri': 'java.util.List', 'kind': 'import'},
            {'uri': 'org.junit.jupiter.api.Assertions.assertEquals', 'kind': 'static'},
        ])
        files = self.resolver.imported_files(outline.path, outline)
        self.assertEqual(files, [
            'src/main/java/shop/util/Money.java', 'src/main/java/shop/util/Tax.java',
            'src/main/java/other/Rates.java',
            'src/main/java/shop/Cart.java', 'src/main/java/shop/Item.java',
        ])

    def test_static_import_resolves_class(self):
        outline = FileOutline('src/main/java/other/Rates.java', imports=[
            {'uri': 'other', 'kind': 'package'},
            {'uri': 'shop.util.Tax.rate', 'kind': 'static'},
        ])
        self.assertEqual(self.resolver.imported_files(outline.path, outline),
                         ['src/main/java/shop/util/Tax.java'])


class FewShotDataTest(unittest.TestCase):
    def test_examples_are_well_formed(self):
        examples = load_examples(FEW_SHOT_EXAMPLES_FILE)
        self.assertTrue(examples)
        for rule, items in examples.items():
            for item in items:
                self.assertNotEqual(item['before'].strip(), item['after'].strip(), rule)

    def test_every_java_example_has_a_curated_description(self):
        import yaml
        examples = load_examples(FEW_SHOT_EXAMPLES_FILE)
        with RULE_DESCRIPTIONS_FILE.open(encoding='utf-8') as f:
            descriptions = yaml.safe_load(f)
        java_rules = {r for r in examples if r.startswith('java:')}
        self.assertTrue(java_rules)
        self.assertEqual(java_rules - set(descriptions), set())


if __name__ == '__main__':
    unittest.main()
