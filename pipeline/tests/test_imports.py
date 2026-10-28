"""Import handling for snippets that start below the file's imports."""

import unittest

from pipeline.common.imports import (file_imports, header_end_line, merge_imports,
                                     remove_unused_imports, split_leading_imports,
                                     used_imports)

JAVA = """\
/*
 * License
 */

package network;

import java.io.IOException;
import java.util.HashMap;
import java.util.StringJoiner;

public class A {
  void f(HashMap<String, String> m) throws IOException {}
}""".splitlines()


class MergeImportsTest(unittest.TestCase):
    def test_sorted_into_existing_group(self):
        lines, added = merge_imports(JAVA, ['import java.util.Map', 'import java.io.IOException;'],
                                     'java')
        self.assertEqual(added, ['import java.util.Map;'])
        self.assertEqual(lines[6:10], ['import java.io.IOException;', 'import java.util.HashMap;',
                                       'import java.util.Map;', 'import java.util.StringJoiner;'])

    def test_first_static_import_gets_its_own_group(self):
        lines, _ = merge_imports(
            JAVA, ['import static org.junit.jupiter.api.Assertions.assertEquals;'], 'java')
        self.assertEqual(lines[5:8], ['', 'import static org.junit.jupiter.api.Assertions.'
                                          'assertEquals;', ''])
        self.assertEqual(lines[8], 'import java.io.IOException;')

    def test_static_imports_stay_sorted(self):
        source = ['package t;', '', 'import static org.junit.Assert.assertTrue;', '',
                  'import org.junit.Test;', '', 'class T {}']
        lines, _ = merge_imports(source, ['import static org.junit.Assert.assertEquals;'],
                                 'java')
        self.assertEqual(lines[2:4], ['import static org.junit.Assert.assertEquals;',
                                      'import static org.junit.Assert.assertTrue;'])

    def test_file_without_imports(self):
        lines, _ = merge_imports(['package x;', '', 'class B {}'], ['import java.util.Map;'],
                                 'java')
        self.assertEqual(lines, ['package x;', '', 'import java.util.Map;', '', 'class B {}'])
        lines, _ = merge_imports(['class B {}'], ['import java.util.Map;'], 'java')
        self.assertEqual(lines, ['import java.util.Map;', '', 'class B {}'])

    def test_dart(self):
        source = ["import 'package:b/b.dart';", '', 'void main() {}']
        lines, added = merge_imports(source, ["import 'package:a/a.dart'"], 'dart')
        self.assertEqual(added, ["import 'package:a/a.dart';"])
        self.assertEqual(lines[:2], ["import 'package:a/a.dart';", "import 'package:b/b.dart';"])

    def test_ignores_non_imports(self):
        self.assertEqual(merge_imports(JAVA, ['Map'], 'java'), (JAVA, []))


class RemoveUnusedTest(unittest.TestCase):
    def test_only_candidates_that_are_unused(self):
        patched = [line.replace('HashMap', 'Map') if 'void f' in line else line for line in JAVA]
        lines, removed = remove_unused_imports(
            patched, {'import java.util.HashMap;', 'import java.io.IOException;'}, 'java')
        self.assertEqual(removed, ['import java.util.HashMap;'])
        self.assertIn('import java.io.IOException;', lines)
        # StringJoiner is unused too, but was not a candidate.
        self.assertIn('import java.util.StringJoiner;', lines)

    def test_emptied_group_leaves_one_blank_line(self):
        source = ['package t;', '', 'import static a.B.c;', '', 'import a.D;', '',
                  'class T { D d; }']
        lines, _ = remove_unused_imports(source, {'import static a.B.c;'}, 'java')
        self.assertEqual(lines, ['package t;', '', 'import a.D;', '', 'class T { D d; }'])

    def test_used_imports(self):
        self.assertEqual(used_imports(JAVA, 'void f(HashMap<String, String> m)', 'java'),
                         {'import java.util.HashMap;'})


class SnippetImportsTest(unittest.TestCase):
    def test_file_imports(self):
        self.assertEqual(file_imports(JAVA, 'java', 11).splitlines()[1],
                         'import java.util.HashMap;')
        self.assertIsNone(file_imports(JAVA, 'java', 8))      # snippet covers imports
        self.assertEqual(file_imports(['class A {}'], 'java', 1), '')
        self.assertEqual(header_end_line(JAVA, 'java'), 9)

    def test_split_leading_imports(self):
        self.assertEqual(split_leading_imports(['import java.util.Map;', '', 'class A {}'],
                                               ['class A {}'], 'java'),
                         (['class A {}'], ['import java.util.Map;']))
        # An S1128 snippet that covers the imports keeps them in the code.
        code = ['import java.util.Map;', 'class A {}']
        self.assertEqual(split_leading_imports(code, ['import java.util.List;'], 'java'),
                         (code, []))


if __name__ == '__main__':
    unittest.main()
