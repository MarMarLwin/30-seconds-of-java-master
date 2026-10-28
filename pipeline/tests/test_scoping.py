import shutil
import unittest
from pathlib import Path

from pipeline.common.ast_outline import Scope
from pipeline.common.dart_ast import DartAstClient
from pipeline.common.java_ast import JavaAstClient, java_major_version
from pipeline.stage1.scoping import (ast_scope, resolve_scope, scope_kind_for,
                                     yaml_step_scope)

WORKFLOW = """\
jobs:
  build:
    steps:
      - uses: actions/checkout@abc # v4
        with:
          fetch-depth: 0
      - name: Set up Flutter
        uses: subosito/flutter-action@v2
        with:
          channel: stable
      # A comment before the next step
      - name: Build
        run: flutter build apk
""".splitlines()

JAVA = """\
package shop;

import java.util.List;

/** Totals. */
@SuppressWarnings("unused")
public class Totals {
  private final int limit = 3;

  public static int sum(List<Integer> values) {
    int total = 0;
    for (int i = 0; i < values.size(); i++) {
      total += values.get(i);
    }
    try {
      check(values);
    } catch (IllegalStateException e) {
      return -1;
    }
    values.forEach(v -> {
      System.out.println(v);
    });
    return total;
  }

  record Line(String sku, int qty) {}

  enum Unit { PIECE, BOX }
}
"""

DART = """\
class Foo {
  int x = 0;

  void bar(BuildContext context) {
    final y = x > 0
        ? 1
        : (x < 0 ? -1 : 0);
    try {
      run();
    } catch (_) {
      print(_);
    }
    onTap(() async {
      await work();
      if (mounted) context.pop();
    });
  }
}
"""


class YamlStepTest(unittest.TestCase):
    def test_step_containing_uses_line(self):
        scope = yaml_step_scope(WORKFLOW, 8, 8)
        self.assertEqual((scope.start_line, scope.end_line, scope.kind), (7, 10, 'yaml_step'))

    def test_first_step(self):
        scope = yaml_step_scope(WORKFLOW, 4, 4)
        self.assertEqual((scope.start_line, scope.end_line), (4, 6))

    def test_last_step(self):
        scope = yaml_step_scope(WORKFLOW, 13, 13)
        self.assertEqual((scope.start_line, scope.end_line), (12, 13))

    def test_outside_steps(self):
        self.assertIsNone(yaml_step_scope(WORKFLOW, 1, 1))


class DartScopeTest(unittest.TestCase):
    SCOPES = [
        Scope('member', 4, 17),
        Scope('statement', 5, 7),
        Scope('argument', 6, 7),      # pretend: a named argument inside the statement
        Scope('catch', 10, 12),
        Scope('function', 13, 16),
        Scope('statement', 15, 15),
    ]

    def test_member(self):
        scope = ast_scope(self.SCOPES, 'member', 4, 4)
        self.assertEqual((scope.start_line, scope.end_line), (4, 17))

    def test_expression_prefers_smallest(self):
        scope = ast_scope(self.SCOPES, 'expression', 7, 7)
        self.assertEqual((scope.start_line, scope.end_line), (6, 7))

    def test_function_prefers_closure(self):
        scope = ast_scope(self.SCOPES, 'function', 15, 15)
        self.assertEqual((scope.start_line, scope.end_line), (13, 16))

    def test_catch(self):
        scope = ast_scope(self.SCOPES, 'catch', 11, 11)
        self.assertEqual((scope.start_line, scope.end_line), (10, 12))

    def test_no_match_returns_none(self):
        self.assertIsNone(ast_scope(self.SCOPES, 'catch', 5, 5))

    def test_rule_mapping(self):
        self.assertEqual(scope_kind_for('dart:S3776', 'lib/a.dart'), 'member')
        self.assertEqual(scope_kind_for('dart:S9999', 'lib/a.dart'), 'statement')
        self.assertEqual(scope_kind_for('githubactions:S7637', '.github/workflows/b.yaml'),
                         'yaml_step')
        self.assertEqual(scope_kind_for('java:S1118', 'src/A.java'), 'type')
        self.assertEqual(scope_kind_for('java:S127', 'src/A.java'), 'loop')
        self.assertEqual(scope_kind_for('java:S9999', 'src/A.java'), 'statement')
        self.assertIsNone(scope_kind_for('java:S1128', 'src/A.java'))
        self.assertIsNone(scope_kind_for('kotlin:S1', 'src/A.kt'))

    def test_falls_back_to_line_buffer(self):
        scope = resolve_scope('java:S1', 'src/A.java', ['x'] * 20, 10, 10, None)
        self.assertEqual((scope.start_line, scope.end_line, scope.kind), (7, 13, 'lines'))


@unittest.skipUnless(shutil.which('dart'), 'dart is not installed')
class DartAstIntegrationTest(unittest.TestCase):
    """Runs the real Dart AST tool on DART above."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / 'lib').mkdir()
        (root / 'lib' / 'foo.dart').write_text(DART)
        cls.ast = DartAstClient(root)
        cls.lines = DART.splitlines()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def scope(self, rule, line):
        return resolve_scope(rule, 'lib/foo.dart', self.lines, line, line, self.ast)

    def test_complexity_takes_whole_method(self):
        s = self.scope('dart:S3776', 4)
        self.assertEqual((s.start_line, s.end_line, s.kind), (4, 17, 'member'))

    def test_nested_ternary_takes_statement(self):
        s = self.scope('dart:S3358', 7)
        self.assertEqual((s.start_line, s.end_line, s.kind), (5, 7, 'expression'))

    def test_wildcard_takes_catch_clause(self):
        s = self.scope('dart:S7055', 11)
        self.assertEqual((s.start_line, s.end_line, s.kind), (10, 12, 'catch'))

    def test_async_gap_takes_closure(self):
        s = self.scope('dart:S7115', 15)
        self.assertEqual((s.start_line, s.end_line, s.kind), (13, 16, 'function'))

    def test_outline_declarations(self):
        outline = self.ast.outline('lib/foo.dart')
        names = {(d.kind, d.name, d.parent) for d in outline.declarations}
        self.assertIn(('class', 'Foo', None), names)
        self.assertIn(('field', 'x', 'Foo'), names)
        self.assertIn(('method', 'bar', 'Foo'), names)


class JavaVersionTest(unittest.TestCase):
    def test_parses_modern_and_legacy_versions(self):
        self.assertEqual(java_major_version('openjdk version "21.0.2" 2024-01-16'), 21)
        self.assertEqual(java_major_version('openjdk version "23" 2024-09-17'), 23)
        self.assertEqual(java_major_version('java version "1.8.0_392"'), 8)
        self.assertEqual(java_major_version('nonsense'), 0)


@unittest.skipUnless(shutil.which('java') and JavaAstClient(Path('.')).available,
                     'JDK 17+ is not installed')
class JavaAstIntegrationTest(unittest.TestCase):
    """Runs the real Java AST tool on JAVA above."""

    PATH = 'src/main/java/shop/Totals.java'

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / 'src/main/java/shop').mkdir(parents=True)
        (root / cls.PATH).write_text(JAVA)
        cls.ast = JavaAstClient(root)
        cls.lines = JAVA.splitlines()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def scope(self, rule, line):
        return resolve_scope(rule, self.PATH, self.lines, line, line, self.ast)

    def test_utility_class_rule_takes_whole_class_without_javadoc(self):
        s = self.scope('java:S1118', 7)
        self.assertEqual((s.start_line, s.end_line, s.kind), (6, 29, 'type'))

    def test_complexity_takes_whole_method(self):
        s = self.scope('java:S3776', 10)
        self.assertEqual((s.start_line, s.end_line, s.kind), (10, 24, 'member'))

    def test_loop_counter_rule_takes_loop(self):
        s = self.scope('java:S127', 13)
        self.assertEqual((s.start_line, s.end_line, s.kind), (12, 14, 'loop'))

    def test_default_takes_innermost_statement(self):
        s = self.scope('java:S9999', 13)
        self.assertEqual((s.start_line, s.end_line, s.kind), (13, 13, 'statement'))
        s = self.scope('java:S1068', 8)
        self.assertEqual((s.start_line, s.end_line, s.kind), (8, 8, 'statement'))

    def test_outline(self):
        outline = self.ast.outline(self.PATH)
        names = {(d.kind, d.name, d.parent) for d in outline.declarations}
        self.assertIn(('class', 'Totals', None), names)
        self.assertIn(('field', 'limit', 'Totals'), names)
        self.assertIn(('method', 'sum', 'Totals'), names)
        self.assertIn(('record', 'Line', 'Totals'), names)
        self.assertIn(('enum_constant', 'BOX', 'Unit'), names)
        signatures = {d.name: d.signature for d in outline.declarations}
        self.assertEqual(signatures['Totals'], 'public class Totals')
        self.assertEqual(signatures['sum'], 'public static int sum(List<Integer> values)')
        self.assertIn({'uri': 'shop', 'kind': 'package'}, outline.imports)
        self.assertIn({'uri': 'java.util.List', 'kind': 'import'}, outline.imports)
        kinds = {s.kind for s in outline.scopes}
        self.assertTrue({'type', 'member', 'statement', 'loop', 'try', 'catch',
                         'function'} <= kinds)


if __name__ == '__main__':
    unittest.main()
