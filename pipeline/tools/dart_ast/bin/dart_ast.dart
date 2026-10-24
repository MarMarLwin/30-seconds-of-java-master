// Dumps a syntactic outline of Dart files as JSON, for Stage 2 of the
// refactoring pipeline (snippet scoping and repository-aware retrieval).
//
// Usage:
//   dart run bin/dart_ast.dart --root <repo> <relative/file.dart> ...
//
// Output (stdout): a JSON list with one entry per file:
//   {
//     "path": "lib/...",
//     "imports": [{"uri": "...", "kind": "import|export|part"}],
//     "declarations": [{"kind", "name", "parent", "start_line", "end_line",
//                       "signature"}],   (members and enum constants have a parent)
//     "scopes": [{"kind", "start_line", "end_line"}]
//   }
//
// Parsing is syntactic only (no type resolution), so it is fast and does not
// need the target project's dependencies to be resolved.

import 'dart:convert';
import 'dart:io';

import 'package:analyzer/dart/analysis/features.dart';
import 'package:analyzer/dart/analysis/utilities.dart';
import 'package:analyzer/dart/ast/ast.dart';
import 'package:analyzer/dart/ast/token.dart';
import 'package:analyzer/dart/ast/visitor.dart';
import 'package:analyzer/source/line_info.dart';

void main(List<String> args) {
  var root = Directory.current.path;
  final files = <String>[];
  for (var i = 0; i < args.length; i++) {
    if (args[i] == '--root' && i + 1 < args.length) {
      root = args[++i];
    } else {
      files.add(args[i]);
    }
  }

  final results = <Map<String, Object?>>[];
  for (final relPath in files) {
    final file = File('$root/$relPath');
    if (!file.existsSync()) {
      stderr.writeln('dart_ast: file not found: $relPath');
      continue;
    }
    final parsed = parseFile(
      path: file.absolute.path,
      featureSet: FeatureSet.latestLanguageVersion(),
      throwIfDiagnostics: false,
    );
    results.add(_Outliner(relPath, parsed.content, parsed.unit).toJson());
  }
  stdout.writeln(jsonEncode(results));
}

class _Outliner {
  _Outliner(this.path, this.content, this.unit) : lineInfo = unit.lineInfo;

  final String path;
  final String content;
  final CompilationUnit unit;
  final LineInfo lineInfo;

  int _line(int offset) => lineInfo.getLocation(offset).lineNumber;

  Map<String, Object?> _range(AstNode node) => {
        'start_line': _line(node.offset),
        'end_line': _line(node.end > node.offset ? node.end - 1 : node.end),
      };

  /// Source text from [start] (skipping doc comments and annotations) up to
  /// [end], with whitespace collapsed so it reads as a one-line signature.
  String _text(Token start, int end) =>
      content.substring(start.offset, end).replaceAll(RegExp(r'\s+'), ' ').trim();

  Map<String, Object?> toJson() {
    final imports = <Map<String, Object?>>[];
    for (final directive in unit.directives) {
      if (directive is ImportDirective) {
        imports.add({'uri': directive.uri.stringValue, 'kind': 'import'});
      } else if (directive is ExportDirective) {
        imports.add({'uri': directive.uri.stringValue, 'kind': 'export'});
      } else if (directive is PartDirective) {
        imports.add({'uri': directive.uri.stringValue, 'kind': 'part'});
      }
    }

    final declarations = <Map<String, Object?>>[];
    for (final decl in unit.declarations) {
      _addDeclaration(decl, declarations);
    }

    final scopes = _ScopeCollector(this);
    unit.accept(scopes);

    return {
      'path': path,
      'imports': imports,
      'declarations': declarations,
      'scopes': scopes.scopes,
    };
  }

  void _add(List<Map<String, Object?>> out, String kind, String name,
      AstNode node, String signature, {String? parent}) {
    out.add({
      'kind': kind,
      'name': name,
      'parent': parent,
      ..._range(node),
      'signature': signature,
    });
  }

  void _addDeclaration(
      CompilationUnitMember decl, List<Map<String, Object?>> out) {
    final first = decl.firstTokenAfterCommentAndMetadata;
    if (decl is ClassDeclaration) {
      final name = decl.name.lexeme;
      _add(out, 'class', name, decl, _text(first, decl.leftBracket.offset));
      _addMembers(decl.members, name, out);
    } else if (decl is MixinDeclaration) {
      final name = decl.name.lexeme;
      _add(out, 'mixin', name, decl, _text(first, decl.leftBracket.offset));
      _addMembers(decl.members, name, out);
    } else if (decl is EnumDeclaration) {
      final name = decl.name.lexeme;
      _add(out, 'enum', name, decl, _text(first, decl.leftBracket.offset));
      for (final constant in decl.constants) {
        _add(out, 'enum_constant', constant.name.lexeme, constant,
            _text(constant.beginToken, constant.end),
            parent: name);
      }
      _addMembers(decl.members, name, out);
    } else if (decl is ExtensionDeclaration) {
      final name = decl.name?.lexeme ?? '<unnamed>';
      _add(out, 'extension', name, decl, _text(first, decl.leftBracket.offset));
      _addMembers(decl.members, name, out);
    } else if (decl is ExtensionTypeDeclaration) {
      final name = decl.name.lexeme;
      _add(out, 'extension_type', name, decl,
          _text(first, decl.leftBracket.offset));
      _addMembers(decl.members, name, out);
    } else if (decl is FunctionDeclaration) {
      final body = decl.functionExpression.body;
      _add(out, 'function', decl.name.lexeme, decl, _text(first, body.offset));
    } else if (decl is TopLevelVariableDeclaration) {
      for (final variable in decl.variables.variables) {
        _add(out, 'variable', variable.name.lexeme, decl,
            _text(first, decl.end));
      }
    } else if (decl is TypeAlias) {
      _add(out, 'typedef', decl.name.lexeme, decl, _text(first, decl.end));
    }
  }

  void _addMembers(List<ClassMember> members, String parent,
      List<Map<String, Object?>> out) {
    for (final member in members) {
      final first = member.firstTokenAfterCommentAndMetadata;
      if (member is MethodDeclaration) {
        final kind = member.isGetter
            ? 'getter'
            : member.isSetter
                ? 'setter'
                : 'method';
        _add(out, kind, member.name.lexeme, member,
            _text(first, member.body.offset),
            parent: parent);
      } else if (member is ConstructorDeclaration) {
        final name = member.name == null
            ? parent
            : '$parent.${member.name!.lexeme}';
        _add(out, 'constructor', name, member,
            _text(first, member.body.offset),
            parent: parent);
      } else if (member is FieldDeclaration) {
        for (final variable in member.fields.variables) {
          _add(out, 'field', variable.name.lexeme, member,
              _text(first, member.end),
              parent: parent);
        }
      }
    }
  }
}

/// Collects every node that can serve as a snippet scope, with its line range.
class _ScopeCollector extends RecursiveAstVisitor<void> {
  _ScopeCollector(this.outliner);

  final _Outliner outliner;
  final scopes = <Map<String, Object?>>[];

  void _add(String kind, AstNode node) =>
      scopes.add({'kind': kind, ...outliner._range(node)});

  @override
  void visitMethodDeclaration(MethodDeclaration node) {
    _add('member', node);
    super.visitMethodDeclaration(node);
  }

  @override
  void visitConstructorDeclaration(ConstructorDeclaration node) {
    _add('member', node);
    super.visitConstructorDeclaration(node);
  }

  @override
  void visitFunctionDeclaration(FunctionDeclaration node) {
    _add('member', node);
    super.visitFunctionDeclaration(node);
  }

  @override
  void visitFunctionExpression(FunctionExpression node) {
    // Top-level and local function declarations are recorded as 'member'.
    if (node.parent is! FunctionDeclaration) _add('function', node);
    super.visitFunctionExpression(node);
  }

  @override
  void visitCatchClause(CatchClause node) {
    _add('catch', node);
    super.visitCatchClause(node);
  }

  @override
  void visitNamedExpression(NamedExpression node) {
    _add('argument', node);
    super.visitNamedExpression(node);
  }

  @override
  void visitVariableDeclarationStatement(VariableDeclarationStatement node) {
    _add('statement', node);
    super.visitVariableDeclarationStatement(node);
  }

  @override
  void visitExpressionStatement(ExpressionStatement node) {
    _add('statement', node);
    super.visitExpressionStatement(node);
  }

  @override
  void visitReturnStatement(ReturnStatement node) {
    _add('statement', node);
    super.visitReturnStatement(node);
  }

  @override
  void visitIfStatement(IfStatement node) {
    _add('statement', node);
    super.visitIfStatement(node);
  }

  @override
  void visitFieldDeclaration(FieldDeclaration node) {
    _add('statement', node);
    super.visitFieldDeclaration(node);
  }
}
