import shutil
import unittest
from pathlib import Path

from pipeline.common.dart_ast import DartAstClient, Scope
from pipeline.stage1.scoping import (dart_scope, resolve_scope, scope_kind_for,
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
        scope = dart_scope(self.SCOPES, 'member', 4, 4)
        self.assertEqual((scope.start_line, scope.end_line), (4, 17))

    def test_expression_prefers_smallest(self):
        scope = dart_scope(self.SCOPES, 'expression', 7, 7)
        self.assertEqual((scope.start_line, scope.end_line), (6, 7))

    def test_function_prefers_closure(self):
        scope = dart_scope(self.SCOPES, 'function', 15, 15)
        self.assertEqual((scope.start_line, scope.end_line), (13, 16))

    def test_catch(self):
        scope = dart_scope(self.SCOPES, 'catch', 11, 11)
        self.assertEqual((scope.start_line, scope.end_line), (10, 12))

    def test_no_match_returns_none(self):
        self.assertIsNone(dart_scope(self.SCOPES, 'catch', 5, 5))

    def test_rule_mapping(self):
        self.assertEqual(scope_kind_for('dart:S3776', 'lib/a.dart'), 'member')
        self.assertEqual(scope_kind_for('dart:S9999', 'lib/a.dart'), 'statement')
        self.assertEqual(scope_kind_for('githubactions:S7637', '.github/workflows/b.yaml'),
                         'yaml_step')
        self.assertIsNone(scope_kind_for('java:S1', 'src/A.java'))

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


if __name__ == '__main__':
    unittest.main()
