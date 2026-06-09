/*
 * Sonar TypeEval harness — runs sonar-python's v2 type inference on a TypeEvalPy
 * snippet directory and writes main_result.json so result_analyzer can score it.
 *
 * Per-snippet flow:
 *   1) Discover every .py file under <snippet-dir>
 *   2) Build a single ProjectLevelTypeTable for the snippet
 *   3) Parse + build symbol table + run TypeInferenceV2 for every file
 *   4) Read main_gt.json; for each GT entry, find the matching Name in main.py's
 *      tree and emit its inferred type in TypeEvalPy's flat-string vocabulary
 */
package com.archway.sonarrunner;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sonar.sslr.api.AstNode;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.stream.Stream;

import org.sonar.plugins.python.api.PythonFile;
import org.sonar.plugins.python.api.symbols.v2.SymbolV2;
import org.sonar.plugins.python.api.symbols.v2.UsageV2;
import org.sonar.plugins.python.api.tree.BaseTreeVisitor;
import org.sonar.plugins.python.api.tree.FileInput;
import org.sonar.plugins.python.api.tree.FunctionDef;
import org.sonar.plugins.python.api.tree.Name;
import org.sonar.plugins.python.api.tree.Parameter;
import org.sonar.plugins.python.api.tree.ReturnStatement;
import org.sonar.plugins.python.api.tree.Token;
import org.sonar.plugins.python.api.tree.Tree;
import org.sonar.plugins.python.api.types.v2.ClassType;
import org.sonar.plugins.python.api.types.v2.FunctionType;
import org.sonar.plugins.python.api.types.v2.ObjectType;
import org.sonar.plugins.python.api.types.v2.PythonType;
import org.sonar.plugins.python.api.types.v2.UnionType;
import org.sonar.python.parser.PythonParser;
import org.sonar.python.semantic.ProjectLevelSymbolTable;
import org.sonar.python.semantic.v2.SymbolTable;
import org.sonar.python.semantic.v2.SymbolTableBuilderV2;
import org.sonar.python.semantic.v2.TypeInferenceV2;
import org.sonar.python.semantic.v2.typetable.ProjectLevelTypeTable;
import org.sonar.python.tree.PythonTreeMaker;

public final class SonarTypeEvalRunner {

  private SonarTypeEvalRunner() {}

  public static void main(String[] args) throws IOException {
    if (args.length < 1) {
      System.err.println("usage: sonar-typeeval-runner <benchmark-root>");
      System.err.println("       sonar-typeeval-runner --dump <snippet-dir>");
      System.exit(2);
    }
    if ("--dump".equals(args[0])) {
      dumpSnippet(Paths.get(args[1]).toAbsolutePath().normalize());
      return;
    }
    Path root = Paths.get(args[0]).toAbsolutePath().normalize();
    int processed = 0;
    int errors = 0;
    List<Path> snippetDirs = findSnippetDirs(root);
    for (Path snippetDir : snippetDirs) {
      try {
        processSnippet(snippetDir);
        processed++;
      } catch (Exception e) {
        errors++;
        System.err.println("error processing " + snippetDir + ": " + e);
      }
    }
    System.out.println("processed=" + processed + " errors=" + errors);
  }

  /** Diagnostic: parse a snippet and print every Name with its inferred type. */
  private static void dumpSnippet(Path snippetDir) throws IOException {
    List<Path> pyFiles;
    try (Stream<Path> stream = Files.walk(snippetDir)) {
      pyFiles = stream.filter(p -> p.getFileName().toString().endsWith(".py")).sorted().toList();
    }
    ProjectLevelSymbolTable projectSymbols = ProjectLevelSymbolTable.empty();
    Map<Path, FileInput> trees = new LinkedHashMap<>();
    Map<Path, PythonFile> files = new LinkedHashMap<>();
    Map<Path, String> packages = new LinkedHashMap<>();
    for (Path py : pyFiles) {
      String source = Files.readString(py, StandardCharsets.UTF_8);
      FileInput fi = new PythonTreeMaker().fileInput(PythonParser.create().parse(source));
      PythonFile pf = new SnippetPythonFile(snippetDir, py, source);
      String pkg = packageNameFor(snippetDir, py);
      projectSymbols.addModule(fi, pkg, pf);
      trees.put(py, fi);
      files.put(py, pf);
      packages.put(py, pkg);
    }
    for (Path py : pyFiles) {
      String key = (packages.get(py).isEmpty() ? "" : packages.get(py) + ".")
          + py.getFileName().toString().replaceFirst("\\.py$", "");
      Set<?> ds = projectSymbols.getDescriptorsFromModule(key);
      System.err.println("[debug] registered: " + key + " descriptors="
          + (ds == null ? "null" : Integer.toString(ds.size())));
    }
    ProjectLevelTypeTable projectTypeTable = new ProjectLevelTypeTable(projectSymbols);
    for (Path py : pyFiles) {
      FileInput fi = trees.get(py);
      SymbolTable st = new SymbolTableBuilderV2(fi).build();
      new TypeInferenceV2(projectTypeTable, files.get(py), st, packages.get(py))
          .inferTypes(fi);
    }
    for (var entry : trees.entrySet()) {
      Path py = entry.getKey();
      FileInput tree = entry.getValue();
      System.out.println("=== " + snippetDir.relativize(py) + " ===");
      tree.accept(new BaseTreeVisitor() {
        @Override
        public void visitName(Name n) {
          Token tok = n.firstToken();
          int line = tok == null ? -1 : tok.line();
          int col = tok == null ? -1 : tok.column() + 1; // 1-indexed for GT parity
          PythonType t = n.typeV2();
          String tdesc = t == null ? "null"
              : t.getClass().getSimpleName() + "(" + safe(t.toString()) + ")";
          System.out.printf("  L%d C%d  %-20s  %s%n", line, col, n.name(), tdesc);
          super.visitName(n);
        }
      });
    }
  }

  private static String safe(String s) {
    if (s == null) return "";
    return s.length() <= 200 ? s : s.substring(0, 200) + "...";
  }

  /** A snippet dir is any directory containing both main.py and main_gt.json. */
  private static List<Path> findSnippetDirs(Path root) throws IOException {
    List<Path> dirs = new ArrayList<>();
    try (Stream<Path> stream = Files.walk(root)) {
      stream.filter(p -> p.getFileName().toString().equals("main_gt.json"))
            .map(Path::getParent)
            .filter(p -> Files.exists(p.resolve("main.py")))
            .forEach(dirs::add);
    }
    Collections.sort(dirs);
    return dirs;
  }

  private static void processSnippet(Path snippetDir) throws IOException {
    List<Path> pyFiles;
    try (Stream<Path> stream = Files.walk(snippetDir)) {
      pyFiles = stream
          .filter(p -> p.getFileName().toString().endsWith(".py"))
          .sorted()
          .toList();
    }
    if (pyFiles.isEmpty()) {
      return;
    }

    ProjectLevelSymbolTable projectSymbols = ProjectLevelSymbolTable.empty();

    // Pass 1: parse every snippet file and register it with the project-level
    // symbol table. Without this, sibling `import to_import` resolves to
    // UnresolvedImportType. addModule walks the file's symbols once.
    Map<Path, FileInput> trees = new LinkedHashMap<>();
    Map<Path, PythonFile> files = new LinkedHashMap<>();
    Map<Path, String> packages = new LinkedHashMap<>();
    for (Path py : pyFiles) {
      String source = Files.readString(py, StandardCharsets.UTF_8);
      FileInput fileInput =
          new PythonTreeMaker().fileInput(PythonParser.create().parse(source));
      PythonFile pyFile = new SnippetPythonFile(snippetDir, py, source);
      String packageName = packageNameFor(snippetDir, py);
      projectSymbols.addModule(fileInput, packageName, pyFile);
      trees.put(py, fileInput);
      files.put(py, pyFile);
      packages.put(py, packageName);
    }

    // Pass 2: build symbol tables and run type inference using the now-populated
    // project symbol table. ProjectLevelTypeTable is constructed AFTER the
    // symbols are registered so sibling-import resolution works.
    if (Boolean.getBoolean("sonar.runner.debug")) {
      for (Path py : pyFiles) {
        String key = (packages.get(py).isEmpty() ? "" : packages.get(py) + ".")
            + py.getFileName().toString().replaceFirst("\\.py$", "");
        Set<?> ds = projectSymbols.getDescriptorsFromModule(key);
        System.err.println("[debug] registered module: " + key + " descriptors="
            + (ds == null ? "null" : Integer.toString(ds.size())));
      }
    }
    ProjectLevelTypeTable projectTypeTable = new ProjectLevelTypeTable(projectSymbols);
    for (Path py : pyFiles) {
      FileInput fileInput = trees.get(py);
      SymbolTable symbolTable = new SymbolTableBuilderV2(fileInput).build();
      new TypeInferenceV2(projectTypeTable, files.get(py), symbolTable, packages.get(py))
          .inferTypes(fileInput);
    }

    Path mainPy = snippetDir.resolve("main.py");
    FileInput mainTree = trees.get(mainPy);
    if (mainTree == null) {
      return;
    }

    Path gtPath = snippetDir.resolve("main_gt.json");
    JsonArray gtArray;
    try (var reader = Files.newBufferedReader(gtPath, StandardCharsets.UTF_8)) {
      gtArray = JsonParser.parseReader(reader).getAsJsonArray();
    }

    JsonArray out = new JsonArray();
    NameIndex index = NameIndex.build(mainTree);
    for (JsonElement el : gtArray) {
      JsonObject gt = el.getAsJsonObject();
      JsonObject pred = predictionFor(gt, index, mainTree);
      if (pred != null) {
        out.add(pred);
      }
    }

    Gson gson = new GsonBuilder().setPrettyPrinting().create();
    Path resultPath = snippetDir.resolve("main_result.json");
    Files.writeString(resultPath, gson.toJson(out), StandardCharsets.UTF_8);
  }

  private static String packageNameFor(Path snippetDir, Path file) {
    Path rel = snippetDir.relativize(file).getParent();
    if (rel == null) return "";
    StringBuilder sb = new StringBuilder();
    for (Path part : rel) {
      if (sb.length() > 0) sb.append('.');
      sb.append(part.toString());
    }
    return sb.toString();
  }

  /** Predict the type for one GT entry by looking up its Name in the tree. */
  private static JsonObject predictionFor(JsonObject gt, NameIndex index, FileInput tree) {
    int line = gt.get("line_number").getAsInt();
    Integer col = gt.has("col_offset") && !gt.get("col_offset").isJsonNull()
        ? gt.get("col_offset").getAsInt() : null;
    String wantName = wantName(gt);
    boolean isReturn = gt.has("function") && !gt.has("variable") && !gt.has("parameter");

    Set<String> types;
    if (isReturn) {
      types = returnTypesFor(gt.get("function").getAsString(), tree);
    } else {
      String baseName = baseOf(wantName);
      List<Accessor> accessors = accessorsOf(wantName);
      Name match = index.find(line, col, baseName);
      if (match == null) return null; // truly missing from the AST — leave out
      PythonType t = match.typeV2();
      for (Accessor a : accessors) {
        t = a.project(t);
        if (t == null) break;
      }
      types = t == null ? new TreeSet<>() : flatten(t);
    }
    // Empty type set means Sonar inferred UNKNOWN — emit an explicit empty
    // list so the scorer records a TYPE_MISS rather than treating the
    // position as unanswered.

    JsonObject pred = gt.deepCopy();
    JsonArray arr = new JsonArray();
    for (String s : types) arr.add(s);
    pred.add("type", arr);
    return pred;
  }

  private static String wantName(JsonObject gt) {
    if (gt.has("variable")) return gt.get("variable").getAsString();
    if (gt.has("parameter")) return gt.get("parameter").getAsString();
    if (gt.has("function")) return gt.get("function").getAsString();
    return null;
  }

  /** Strip subscript / attribute suffixes from a GT name. `m['a']` -> `m`. */
  private static String baseOf(String name) {
    if (name == null) return null;
    int lo = name.length();
    for (char sep : new char[] {'[', '.'}) {
      int i = name.indexOf(sep);
      if (i >= 0 && i < lo) lo = i;
    }
    return name.substring(0, lo);
  }

  /** Parse `a[0]`, `b['k']`, `obj.field`, `a.b.c` into an ordered access chain. */
  private static List<Accessor> accessorsOf(String name) {
    List<Accessor> out = new ArrayList<>();
    if (name == null) return out;
    int i = name.length();
    for (char sep : new char[] {'[', '.'}) {
      int idx = name.indexOf(sep);
      if (idx >= 0 && idx < i) i = idx;
    }
    String rest = name.substring(i);
    while (!rest.isEmpty()) {
      if (rest.startsWith("[")) {
        int close = rest.indexOf(']');
        if (close < 0) break;
        out.add(Accessor.SUBSCRIPT);
        rest = rest.substring(close + 1);
      } else if (rest.startsWith(".")) {
        int next = rest.length();
        for (char sep : new char[] {'[', '.'}) {
          int j = rest.indexOf(sep, 1);
          if (j >= 0 && j < next) next = j;
        }
        String attr = rest.substring(1, next);
        out.add(new Accessor(Accessor.Kind.ATTR, attr));
        rest = rest.substring(next);
      } else {
        break;
      }
    }
    return out;
  }

  /** Project a type through one subscript or attribute step. */
  private static final class Accessor {
    enum Kind { SUBSCRIPT, ATTR }
    static final Accessor SUBSCRIPT = new Accessor(Kind.SUBSCRIPT, null);
    final Kind kind;
    final String attr;
    Accessor(Kind kind, String attr) { this.kind = kind; this.attr = attr; }

    PythonType project(PythonType t) {
      if (t == null) return null;
      if (kind == Kind.SUBSCRIPT) {
        // Sonar's ObjectType for list/tuple carries element types in
        // `attributes`; dict carries [key, value]. Note: ObjectType's
        // unwrappedType() returns the bare ClassType — losing the
        // attributes — so we test the original `t` for ObjectType, not
        // its unwrapped form.
        if (t instanceof ObjectType o) {
          List<PythonType> attrs = o.attributes();
          PythonType inner = o.type();
          String cls = inner instanceof ClassType c ? c.name() : "";
          if (attrs != null && !attrs.isEmpty()) {
            if ("dict".equals(cls) && attrs.size() >= 2) {
              return attrs.get(1);
            }
            return attrs.get(0);
          }
        }
        return null;
      } else { // ATTR
        PythonType u = t.unwrappedType();
        if (u != null) {
          return u.resolveMember(attr).orElse(null);
        }
        return null;
      }
    }
  }

  /** Union observed return types across every ReturnStatement in <fn>. */
  private static Set<String> returnTypesFor(String fnName, FileInput tree) {
    Set<String> out = new TreeSet<>();
    new BaseTreeVisitor() {
      @Override
      public void visitFunctionDef(FunctionDef fn) {
        if (fn.name() != null && fnName.equals(fn.name().name())) {
          fn.body().accept(new BaseTreeVisitor() {
            @Override
            public void visitFunctionDef(FunctionDef nested) { /* don't descend */ }
            @Override
            public void visitReturnStatement(ReturnStatement ret) {
              if (ret.expressions() != null) {
                for (var expr : ret.expressions()) {
                  out.addAll(flatten(expr.typeV2()));
                }
              } else {
                out.add("None");
              }
            }
          });
        }
        super.visitFunctionDef(fn);
      }
    }.visitFileInput(tree);
    return out;
  }

  /** Index every Name in the tree by (line, col, identifier). */
  private static final class NameIndex {
    private final List<Name> all = new ArrayList<>();

    static NameIndex build(FileInput tree) {
      NameIndex idx = new NameIndex();
      tree.accept(new BaseTreeVisitor() {
        @Override
        public void visitName(Name n) {
          idx.all.add(n);
          super.visitName(n);
        }
        @Override
        public void visitParameter(Parameter p) {
          if (p.name() != null) idx.all.add(p.name());
          super.visitParameter(p);
        }
      });
      return idx;
    }

    Name find(int line, Integer col, String wantName) {
      // Try strict (line + col + name); fall back to line + name.
      Name best = null;
      for (Name n : all) {
        Token tok = n.firstToken();
        if (tok == null) continue;
        if (tok.line() != line) continue;
        if (!nameMatches(n, wantName)) continue;
        if (col != null && tok.column() + 1 != col) continue;
        best = n;
        break;
      }
      if (best != null) return best;
      for (Name n : all) {
        Token tok = n.firstToken();
        if (tok == null) continue;
        if (tok.line() != line) continue;
        if (nameMatches(n, wantName)) return n;
      }
      return null;
    }

    private static boolean nameMatches(Name n, String want) {
      if (want == null) return true;
      return want.equals(n.name());
    }
  }

  /** Flatten a Sonar PythonType into TypeEvalPy's flat-string vocabulary. */
  private static Set<String> flatten(PythonType t) {
    Set<String> out = new TreeSet<>();
    collect(t, out, new HashSet<>());
    return out;
  }

  private static void collect(PythonType t, Set<String> out, Set<PythonType> seen) {
    if (t == null || t == PythonType.UNKNOWN) return;
    // Force lazily-resolved types (e.g. bool, lazily-loaded typeshed classes)
    // so we walk the resolved class, not the LazyTypeWrapper.
    PythonType unwrapped = t.unwrappedType();
    if (unwrapped != t && unwrapped != null) {
      collect(unwrapped, out, seen);
      return;
    }
    if (!seen.add(t)) return;
    if (t instanceof UnionType u) {
      for (PythonType m : u.candidates()) collect(m, out, seen);
      return;
    }
    if (t instanceof ObjectType o) {
      collect(o.type(), out, seen);
      return;
    }
    if (t instanceof FunctionType) {
      out.add("callable");
      return;
    }
    if (t instanceof ClassType c) {
      String mapped = mapTypeName(c.name());
      if (mapped != null) out.add(mapped);
      return;
    }
    // ModuleType, anything else — ignore.
  }

  /** Map Sonar's class name onto TypeEvalPy's flat-string vocabulary. */
  private static String mapTypeName(String name) {
    if (name == null || name.isEmpty()) return null;
    String tail = name.contains(".") ? name.substring(name.lastIndexOf('.') + 1) : name;
    String lower = tail.toLowerCase(Locale.ROOT);
    return switch (lower) {
      case "int", "str", "float", "bool", "bytes", "complex" -> lower;
      case "list", "dict", "tuple", "set", "frozenset" -> lower;
      // TypeEvalPy's GT spells the None singleton's type as "Nonetype" (mixed
      // case). Keep matching for parity with the scorer.
      case "nonetype", "none" -> "Nonetype";
      default -> tail;
    };
  }

  /** Minimal PythonFile pointing at the snippet on disk. */
  private static final class SnippetPythonFile implements PythonFile {
    private final Path snippetRoot;
    private final Path file;
    private final String content;

    SnippetPythonFile(Path snippetRoot, Path file, String content) {
      this.snippetRoot = snippetRoot;
      this.file = file;
      this.content = content;
    }

    @Override public String content() { return content; }
    @Override public String fileName() { return file.getFileName().toString(); }
    @Override public URI uri() { return file.toUri(); }

    @Override
    public String key() {
      return snippetRoot.relativize(file).toString().replace('\\', '/');
    }
  }
}
