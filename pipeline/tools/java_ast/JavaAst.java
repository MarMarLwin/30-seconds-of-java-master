// Dumps a syntactic outline of Java files as JSON, for the refactoring
// pipeline (snippet scoping and repository-aware retrieval). The Java
// counterpart of tools/dart_ast, with the same output format.
//
// Usage (JDK 17+, no build step or dependencies):
//   java JavaAst.java --root <repo> <relative/File.java> ...
//
// Output (stdout): a JSON list with one entry per file:
//   {
//     "path": "src/main/java/...",
//     "imports": [{"uri": "com.x.Foo" | "com.x.*", "kind": "import|static|package"}],
//     "declarations": [{"kind", "name", "parent", "start_line", "end_line",
//                       "signature"}],   (members and nested types have a parent)
//     "scopes": [{"kind", "start_line", "end_line"}]
//   }
//
// Parsing uses the JDK's own compiler (com.sun.source.tree), syntax only: no
// type resolution and no classpath, so it is fast and accepts any Java syntax
// the running JDK knows.

import com.sun.source.tree.BlockTree;
import com.sun.source.tree.CaseTree;
import com.sun.source.tree.CatchTree;
import com.sun.source.tree.ClassTree;
import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.DoWhileLoopTree;
import com.sun.source.tree.EnhancedForLoopTree;
import com.sun.source.tree.ForLoopTree;
import com.sun.source.tree.ImportTree;
import com.sun.source.tree.LambdaExpressionTree;
import com.sun.source.tree.MethodTree;
import com.sun.source.tree.ModifiersTree;
import com.sun.source.tree.StatementTree;
import com.sun.source.tree.Tree;
import com.sun.source.tree.TryTree;
import com.sun.source.tree.VariableTree;
import com.sun.source.tree.WhileLoopTree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreePathScanner;
import com.sun.source.util.Trees;
import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

public class JavaAst {

  public static void main(String[] args) throws IOException {
    String root = ".";
    List<String> files = new ArrayList<>();
    for (int i = 0; i < args.length; i++) {
      if (args[i].equals("--root") && i + 1 < args.length) {
        root = args[++i];
      } else {
        files.add(args[i]);
      }
    }

    JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
    if (compiler == null) {
      System.err.println("java_ast: no system Java compiler (a JRE without javac?)");
      System.exit(2);
    }
    List<Object> results = new ArrayList<>();
    try (StandardJavaFileManager fm =
        compiler.getStandardFileManager(null, null, StandardCharsets.UTF_8)) {
      for (String relPath : files) {
        File file = new File(root, relPath);
        if (!file.isFile()) {
          System.err.println("java_ast: file not found: " + relPath);
          continue;
        }
        // One task per file, so a file the parser chokes on cannot affect others.
        Iterable<? extends JavaFileObject> sources = fm.getJavaFileObjects(file);
        JavacTask task = (JavacTask) compiler.getTask(
            null, fm, diagnostic -> { }, List.of("-proc:none"), null, sources);
        for (CompilationUnitTree unit : task.parse()) {
          String content = unit.getSourceFile().getCharContent(true).toString();
          results.add(new Outliner(relPath, content, unit, Trees.instance(task)).toJson());
        }
      }
    }
    System.out.println(Json.write(results));
  }

  static final class Outliner {
    final String path;
    final String content;
    final CompilationUnitTree unit;
    final SourcePositions positions;
    final List<Object> declarations = new ArrayList<>();
    final List<Object> scopes = new ArrayList<>();

    Outliner(String path, String content, CompilationUnitTree unit, Trees trees) {
      this.path = path;
      this.content = content;
      this.unit = unit;
      this.positions = trees.getSourcePositions();
    }

    long start(Tree node) {
      return positions.getStartPosition(unit, node);
    }

    long end(Tree node) {
      return positions.getEndPosition(unit, node);
    }

    int line(long offset) {
      return (int) unit.getLineMap().getLineNumber(offset);
    }

    Map<String, Object> range(Tree node) {
      Map<String, Object> out = new LinkedHashMap<>();
      long start = start(node);
      long end = end(node);
      out.put("start_line", line(start));
      out.put("end_line", line(end > start ? end - 1 : start));
      return out;
    }

    /** Where a declaration's signature starts: after its annotations. */
    long signatureStart(Tree node, ModifiersTree modifiers) {
      long start = start(node);
      if (modifiers != null) {
        for (Tree annotation : modifiers.getAnnotations()) {
          start = Math.max(start, end(annotation));
        }
      }
      while (start < content.length() && Character.isWhitespace(content.charAt((int) start))) {
        start++;
      }
      return start;
    }

    /** Source text in [start, end), whitespace collapsed to a one-line signature. */
    String text(long start, long end) {
      if (end <= start) {
        return "";
      }
      return content.substring((int) start, (int) end).replaceAll("\\s+", " ").trim();
    }

    Map<String, Object> toJson() {
      List<Object> imports = new ArrayList<>();
      if (unit.getPackageName() != null) {
        imports.add(Map.of("uri", unit.getPackageName().toString(), "kind", "package"));
      }
      for (ImportTree imp : unit.getImports()) {
        imports.add(Map.of("uri", imp.getQualifiedIdentifier().toString(),
            "kind", imp.isStatic() ? "static" : "import"));
      }
      for (Tree decl : unit.getTypeDecls()) {
        if (decl instanceof ClassTree type) {
          addType(type, null);
        }
      }
      new ScopeCollector().scan(unit, null);

      Map<String, Object> out = new LinkedHashMap<>();
      out.put("path", path);
      out.put("imports", imports);
      out.put("declarations", declarations);
      out.put("scopes", scopes);
      return out;
    }

    void add(String kind, String name, String parent, Tree node, String signature) {
      Map<String, Object> out = new LinkedHashMap<>();
      out.put("kind", kind);
      out.put("name", name);
      out.put("parent", parent);
      out.putAll(range(node));
      out.put("signature", signature);
      declarations.add(out);
    }

    static String typeKind(ClassTree type) {
      return switch (type.getKind().name()) {
        case "INTERFACE" -> "interface";
        case "ENUM" -> "enum";
        case "RECORD" -> "record";
        case "ANNOTATION_TYPE" -> "annotation";
        default -> "class";
      };
    }

    void addType(ClassTree type, String parent) {
      String name = type.getSimpleName().toString();
      long sigStart = signatureStart(type, type.getModifiers());
      int brace = content.indexOf('{', (int) sigStart);
      add(typeKind(type), name, parent, type, text(sigStart, brace < 0 ? end(type) : brace));

      for (Tree member : type.getMembers()) {
        if (member instanceof MethodTree method) {
          addMethod(method, name);
        } else if (member instanceof VariableTree variable) {
          addVariable(variable, name, type.getKind().name().equals("ENUM"));
        } else if (member instanceof ClassTree nested) {
          addType(nested, name);
        }
      }
    }

    void addMethod(MethodTree method, String parent) {
      boolean constructor = method.getName().contentEquals("<init>");
      long sigStart = signatureStart(method, method.getModifiers());
      long sigEnd = method.getBody() != null ? start(method.getBody()) : end(method);
      String signature = text(sigStart, sigEnd);
      if (signature.endsWith(";")) {
        signature = signature.substring(0, signature.length() - 1).trim();
      }
      add(constructor ? "constructor" : "method",
          constructor ? parent : method.getName().toString(), parent, method, signature);
    }

    void addVariable(VariableTree variable, String parent, boolean inEnum) {
      String name = variable.getName().toString();
      long sigStart = signatureStart(variable, variable.getModifiers());
      // An enum constant starts with its own name; a field starts with its type.
      int after = (int) sigStart + name.length();
      if (inEnum && content.startsWith(name, (int) sigStart)
          && (after >= content.length()
              || !Character.isJavaIdentifierPart(content.charAt(after)))) {
        add("enum_constant", name, parent, variable, text(sigStart, end(variable)));
        return;
      }
      String signature = text(sigStart, end(variable));
      if (!signature.endsWith(";")) {
        signature += ";";
      }
      add("field", name, parent, variable, signature);
    }

    /** Every node that can serve as a snippet scope, with its line range. */
    final class ScopeCollector extends TreePathScanner<Void, Void> {
      void scope(String kind, Tree node) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("kind", kind);
        out.putAll(range(node));
        scopes.add(out);
      }

      void statements(List<? extends StatementTree> statements) {
        if (statements != null) {
          for (StatementTree statement : statements) {
            scope("statement", statement);
          }
        }
      }

      @Override
      public Void visitImport(ImportTree node, Void p) {
        scope("statement", node);
        return super.visitImport(node, p);
      }

      @Override
      public Void visitClass(ClassTree node, Void p) {
        scope("type", node);
        for (Tree member : node.getMembers()) {
          if (member instanceof VariableTree) {
            scope("statement", member);
          } else if (member instanceof BlockTree) {
            scope("member", member);  // initializer block
          }
        }
        return super.visitClass(node, p);
      }

      @Override
      public Void visitMethod(MethodTree node, Void p) {
        scope("member", node);
        return super.visitMethod(node, p);
      }

      @Override
      public Void visitLambdaExpression(LambdaExpressionTree node, Void p) {
        scope("function", node);
        return super.visitLambdaExpression(node, p);
      }

      @Override
      public Void visitBlock(BlockTree node, Void p) {
        statements(node.getStatements());
        return super.visitBlock(node, p);
      }

      @Override
      public Void visitCase(CaseTree node, Void p) {
        statements(node.getStatements());
        return super.visitCase(node, p);
      }

      @Override
      public Void visitCatch(CatchTree node, Void p) {
        scope("catch", node);
        return super.visitCatch(node, p);
      }

      @Override
      public Void visitTry(TryTree node, Void p) {
        scope("try", node);
        return super.visitTry(node, p);
      }

      @Override
      public Void visitForLoop(ForLoopTree node, Void p) {
        scope("loop", node);
        return super.visitForLoop(node, p);
      }

      @Override
      public Void visitEnhancedForLoop(EnhancedForLoopTree node, Void p) {
        scope("loop", node);
        return super.visitEnhancedForLoop(node, p);
      }

      @Override
      public Void visitWhileLoop(WhileLoopTree node, Void p) {
        scope("loop", node);
        return super.visitWhileLoop(node, p);
      }

      @Override
      public Void visitDoWhileLoop(DoWhileLoopTree node, Void p) {
        scope("loop", node);
        return super.visitDoWhileLoop(node, p);
      }
    }
  }

  /** Minimal JSON writer for maps, lists, strings, numbers and null. */
  static final class Json {
    static String write(Object value) {
      StringBuilder out = new StringBuilder();
      write(value, out);
      return out.toString();
    }

    static void write(Object value, StringBuilder out) {
      if (value == null) {
        out.append("null");
      } else if (value instanceof Number) {
        out.append(value);
      } else if (value instanceof Map<?, ?> map) {
        out.append('{');
        boolean first = true;
        for (Map.Entry<?, ?> entry : map.entrySet()) {
          if (!first) {
            out.append(',');
          }
          first = false;
          string(entry.getKey().toString(), out);
          out.append(':');
          write(entry.getValue(), out);
        }
        out.append('}');
      } else if (value instanceof List<?> list) {
        out.append('[');
        for (int i = 0; i < list.size(); i++) {
          if (i > 0) {
            out.append(',');
          }
          write(list.get(i), out);
        }
        out.append(']');
      } else {
        string(value.toString(), out);
      }
    }

    static void string(String s, StringBuilder out) {
      out.append('"');
      for (char c : s.toCharArray()) {
        switch (c) {
          case '"' -> out.append("\\\"");
          case '\\' -> out.append("\\\\");
          case '\n' -> out.append("\\n");
          case '\r' -> out.append("\\r");
          case '\t' -> out.append("\\t");
          default -> {
            if (c < 0x20) {
              out.append(String.format("\\u%04x", (int) c));
            } else {
              out.append(c);
            }
          }
        }
      }
      out.append('"');
    }
  }
}
