"""pyright runner — reveal_type injection -> `pyright --outputjson` -> main_result.json.

Replaces the previous LSP-based runner (per-snippet didOpen/didClose forced
pyright to re-analyze from scratch each time, causing >40min hangs on micro
and silent failures on autogen).

Per snippet:
  1. Parse main.py + main_gt.json with `ast`.
  2. Build a transformed copy of main.py that inserts one `reveal_type(...)`
     per GT entry, recording the (inserted_line_number -> gt_index) map.
  3. Run `pyright --outputjson transformed.py`.
  4. Walk the `generalDiagnostics` array; for each diagnostic of severity
     "information" whose `message` matches `Type of "X" is "Y"`, look up its
     start line in the inserted map and map pyright's type string onto
     TypeEvalPy's flat vocabulary.
  5. Write main_result.json next to main.py.

We never modify main.py. The transformed file lives in /tmp/pyright_work/
so multiple parallel pyright invocations stay isolated.
"""
from __future__ import annotations

import argparse
import ast
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import utils

logger = logging.getLogger("pyright-runner")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logger.addHandler(handler)

PYRIGHT_BIN = "pyright"

# Pyright `--outputjson` message shape for reveal_type:
#   "Type of \"x\" is \"int\""
TYPE_OF_RE = re.compile(r'^Type of\s+"([^"]+)"\s+is\s+"(.*)"\s*$')


_SYNTHETIC_MAP = {"None": "Nonetype", "Generator": "generator"}


@dataclass
class Insertion:
    """Where to insert a `reveal_type(<expr>)` for one GT entry."""

    after_line: int          # insert AFTER this 1-indexed source line
    indent: str              # leading whitespace for the inserted line
    expr: str                # what to wrap in reveal_type(...)
    gt_index: int            # which GT entry this insertion serves


@dataclass
class FnInfo:
    name: str
    params: dict[str, ast.arg] = field(default_factory=dict)
    body_first_line: int = -1
    body_indent: str = ""
    returns: list[ast.Return] = field(default_factory=list)
    has_yields: bool = False


def main_runner(benchmark_path: str) -> int:
    root = Path(benchmark_path).resolve()
    gt_files = sorted(root.rglob("main_gt.json"))
    logger.info(f"pyright runner sweeping {len(gt_files)} snippets under {root}")
    processed = 0
    errors = 0
    for gt_path in gt_files:
        snippet = gt_path.parent
        try:
            process_snippet(snippet)
            processed += 1
        except Exception as e:
            errors += 1
            logger.warning(f"snippet {snippet.relative_to(root)} failed: {e}")
    logger.info(f"processed={processed} errors={errors}")
    return 0


def process_snippet(snippet_dir: Path) -> None:
    main_py = snippet_dir / "main.py"
    gt_path = snippet_dir / "main_gt.json"
    if not main_py.exists():
        return

    source = main_py.read_text()
    try:
        tree = ast.parse(source, filename=str(main_py))
    except SyntaxError as e:
        logger.warning(f"{main_py.relative_to(snippet_dir)} syntax: {e}")
        return

    gt = json.loads(gt_path.read_text())
    fns = collect_functions(tree)

    insertions: list[Insertion] = []
    for i, entry in enumerate(gt):
        ins = plan_insertion(entry, fns, source, tree, i)
        if ins is not None:
            insertions.extend(ins)

    transformed, line_to_gt, synthetic = render_transformed(source, insertions)
    if not insertions:
        (snippet_dir / "main_result.json").write_text("[]\n")
        return

    work_root = Path(tempfile.mkdtemp(prefix="pyright_work_"))
    try:
        shutil.copytree(snippet_dir, work_root / "snippet", dirs_exist_ok=True)
        work_main = work_root / "snippet" / "main.py"
        work_main.write_text(transformed)
        pyright_payload = run_pyright(work_main)
    finally:
        shutil.rmtree(work_root, ignore_errors=True)

    predictions = parse_pyright_payload(pyright_payload, line_to_gt, gt, synthetic)
    (snippet_dir / "main_result.json").write_text(json.dumps(predictions, indent=2) + "\n")


def collect_functions(tree: ast.Module) -> dict[str, FnInfo]:
    """Walk every FunctionDef + AsyncFunctionDef; index by qualified name
    (e.g. ``MyClass.func``) to match TypeEvalPy GT's ``function:`` field."""
    out: dict[str, FnInfo] = {}

    def record(node: ast.AST, qual: str) -> None:
        info = FnInfo(name=qual)
        for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
            info.params[a.arg] = a
        if node.args.vararg:
            info.params[node.args.vararg.arg] = node.args.vararg
        if node.args.kwarg:
            info.params[node.args.kwarg.arg] = node.args.kwarg
        if node.body:
            info.body_first_line = node.body[0].lineno
            info.body_indent = " " * node.body[0].col_offset
        info.returns.extend(_returns_of(node))
        info.has_yields = _has_yield(node)
        out[qual] = info
        bare = qual.rsplit(".", 1)[-1]
        out.setdefault(bare, info)

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}"
                record(child, qual)
                walk(child, f"{qual}.")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")

    walk(tree, "")
    return out


def _returns_of(fn: ast.AST) -> list[ast.Return]:
    """Returns belonging DIRECTLY to ``fn`` — not nested fns/lambdas."""
    out: list[ast.Return] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Return):
                out.append(child)
            walk(child)

    walk(fn)
    return out


def _has_yield(fn: ast.AST) -> bool:
    """True if fn's body contains a yield or yield-from (excluding nested
    functions / lambdas, whose yields belong to them)."""
    found = [False]
    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, (ast.Yield, ast.YieldFrom)):
                found[0] = True
                return
            walk(child)
            if found[0]:
                return
    walk(fn)
    return found[0]


def _resolve_variable_position(
    tree: ast.Module, line: int, name: str, source: str
) -> tuple[int, str]:
    """Find where to insert a reveal_type for a variable defined at GT line.

    GT reports the *first* line a variable's binding statement begins on; for
    multi-line list/dict/call assignments and for-loop binders, that's not a
    safe place to inject `reveal_type(<name>)`:

      - ``data = [\\n  {...},\\n  ...\\n]`` -- inserting after the first line
        puts the call inside the open list literal. Use the assignment's
        ``end_lineno`` instead.
      - ``for city, group in data:\\n    ...`` -- the for-target binds inside
        the loop body. Insert before the body's first statement at body
        indent, not at the for-statement's indent.

    Returns (after_line, indent). Falls back to GT line + same-line indent for
    cases the AST doesn't cleanly disambiguate.
    """
    default = (line, leading_indent(source, line))
    for node in ast.walk(tree):
        node_line = getattr(node, "lineno", None)
        if node_line != line:
            continue
        # Multi-line assignment: insert after the closing punctuation.
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            end = getattr(node, "end_lineno", None)
            if end and end > line:
                return end, leading_indent(source, line)
        # For-loop target binds inside the body.
        if isinstance(node, (ast.For, ast.AsyncFor)) and _target_binds_name(node.target, name):
            if node.body:
                first = node.body[0]
                return first.lineno - 1, " " * first.col_offset
        # With-statement target (``with foo as x:``) — same body-scope rule.
        if isinstance(node, ast.With) and node.body:
            for item in node.items:
                if item.optional_vars is not None and _target_binds_name(item.optional_vars, name):
                    first = node.body[0]
                    return first.lineno - 1, " " * first.col_offset
    return default


def _target_binds_name(target: ast.AST, name: str) -> bool:
    """True if ``name`` appears in an assignment-target subtree."""
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, ast.Starred):
        return _target_binds_name(target.value, name)
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_target_binds_name(t, name) for t in target.elts)
    return False


def plan_insertion(
    entry: dict, fns: dict[str, FnInfo], source: str, tree: ast.Module, gt_index: int
) -> list[Insertion] | None:
    """Decide where (and what) to reveal for one GT entry."""
    kind, name, fn_name = entry_kind(entry)
    if kind is None:
        return None

    line = entry.get("line_number")
    if kind == "variable":
        after_line, indent = _resolve_variable_position(tree, line, name, source)
        return [Insertion(after_line=after_line, indent=indent, expr=name, gt_index=gt_index)]
    if kind == "parameter":
        fn = fns.get(fn_name)
        if fn is None or fn.body_first_line < 0:
            return None
        return [Insertion(after_line=fn.body_first_line - 1, indent=fn.body_indent,
                          expr=name, gt_index=gt_index)]
    if kind == "return":
        fn = fns.get(fn_name)
        if fn is None:
            return None
        if not fn.returns:
            # No explicit return: generator functions yield, all others return None.
            synth_expr = "Generator" if fn.has_yields else "None"
            return [Insertion(after_line=-1, indent="", expr=synth_expr,
                              gt_index=gt_index)]
        out: list[Insertion] = []
        for ret in fn.returns:
            if ret.value is None:
                expr = "None"
            else:
                expr = ast.unparse(ret.value)
            out.append(Insertion(after_line=ret.lineno - 1,
                                 indent=" " * ret.col_offset,
                                 expr=expr, gt_index=gt_index))
        return out
    return None


def entry_kind(entry: dict) -> tuple[str | None, str | None, str | None]:
    if "variable" in entry:
        return "variable", entry["variable"], entry.get("function")
    if "parameter" in entry:
        return "parameter", entry["parameter"], entry.get("function")
    if "function" in entry and "variable" not in entry and "parameter" not in entry:
        return "return", entry["function"], entry["function"]
    return None, None, None


def leading_indent(source: str, line: int) -> str:
    lines = source.splitlines()
    if 1 <= line <= len(lines):
        raw = lines[line - 1]
        return raw[: len(raw) - len(raw.lstrip())]
    return ""


def render_transformed(
    source: str, insertions: list[Insertion]
) -> tuple[str, dict[int, int], dict[int, list[str]]]:
    """Insert each reveal_type call after its target line. Returns:
      - the transformed source,
      - {transformed_line_number -> gt_index} for parser lookup,
      - {gt_index -> [pre-baked types]} for synthetic Insertions (after_line=-1)
        that bypass pyright (e.g., functions with no `return` -> None).
    """
    lines = source.splitlines()
    by_line: dict[int, list[Insertion]] = {}
    synthetic: dict[int, list[str]] = {}
    for ins in insertions:
        if ins.after_line < 0:
            synthetic.setdefault(ins.gt_index, []).append(
                _SYNTHETIC_MAP.get(ins.expr, ins.expr)
            )
            continue
        by_line.setdefault(ins.after_line, []).append(ins)

    out: list[str] = []
    line_to_gt: dict[int, int] = {}
    for src_line_no, line in enumerate(lines, start=1):
        out.append(line)
        for ins in by_line.get(src_line_no, []):
            out.append(f"{ins.indent}reveal_type({ins.expr})")
            line_to_gt[len(out)] = ins.gt_index
    return "\n".join(out) + "\n", line_to_gt, synthetic


def run_pyright(path: Path) -> dict:
    """Run pyright on a single file in JSON mode. Returns the parsed payload.
    Pyright exits non-zero when diagnostics fire (and reveal_type is always a
    diagnostic), so we ignore the return code and parse from stdout."""
    proc = subprocess.run(
        [PYRIGHT_BIN, "--outputjson", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if not proc.stdout:
        return {"generalDiagnostics": []}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning(f"pyright JSON parse failed for {path}; stderr: {proc.stderr[:200]}")
        return {"generalDiagnostics": []}


def parse_pyright_payload(
    payload: dict,
    line_to_gt: dict[int, int],
    gt: list[dict],
    synthetic: dict[int, list[str]],
) -> list[dict]:
    """For each `Type of "X" is "Y"` info diagnostic, find its GT entry and
    emit a prediction record. Synthetic predictions get merged in directly."""
    revealed: dict[int, list[str]] = {}

    for diag in payload.get("generalDiagnostics", []):
        if diag.get("severity") != "information":
            continue
        m = TYPE_OF_RE.match(diag.get("message", ""))
        if not m:
            continue
        ty_line = diag.get("range", {}).get("start", {}).get("line")
        if ty_line is None:
            continue
        # Pyright's range.start.line is 0-indexed; our line_to_gt is 1-indexed.
        gt_index = line_to_gt.get(ty_line + 1)
        if gt_index is None:
            continue
        types = flatten_pyright_type(m.group(2))
        revealed.setdefault(gt_index, []).extend(types)

    for gt_index, types in synthetic.items():
        revealed.setdefault(gt_index, []).extend(types)

    out: list[dict] = []
    for i, entry in enumerate(gt):
        if i not in revealed:
            continue
        types = sorted(set(revealed[i]))
        rec = dict(entry)
        rec["type"] = types
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Map pyright type strings -> TypeEvalPy flat vocabulary
# ---------------------------------------------------------------------------

# Pyright forms (selected):
#   `int`, `str`, `bool`, `list[int]`, `dict[str, int]`, `tuple[int, str]`,
#   `Literal[42]`, `Literal["hello"]`, `Literal[1, "hello"]`, `Literal[True]`,
#   `int | str`, `Unknown`, `Any`, `None`,
#   `() -> Literal[42]` / `(x: int) -> str` (callable signatures),
#   `LiteralString`, `Self@Foo`.

_LITERAL_RE = re.compile(r"^Literal\[(.*)\]$")
_GENERIC_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\[")
_LITERAL_VALUE_TO_TYPE = {
    "True": "bool",
    "False": "bool",
    "None": "Nonetype",
}


def flatten_pyright_type(text: str) -> list[str]:
    text = text.strip()
    # Pyright wraps callables in parens when they appear as union members:
    # `(() -> Literal[5]) | Unbound`. Strip any depth-balanced outer pair so
    # callable detection sees the unwrapped form. (Same form pyrefly uses.)
    text = _strip_outer_parens(text)
    # `Unbound` is pyright's marker for a variable that isn't (yet) in scope at
    # the inspected position — typically produced by our harness when an
    # injected reveal_type lands inside a multi-line literal or at the wrong
    # indent level. Treat as no information rather than emitting the literal
    # string "Unbound".
    if not text or text in ("Unknown", "Any", "Never", "Unbound"):
        return []
    if text == "None":
        return ["Nonetype"]
    if text == "LiteralString":
        return ["str"]
    if text.startswith("Self@"):
        head = text[len("Self@"):].split(" ", 1)[0].strip()
        return [normalize_class_name(head)]
    # Callable forms: top-level " -> " at depth 0.
    if _looks_like_callable(text):
        return ["callable"]
    if text.startswith("type[") and text.endswith("]"):
        return ["type"]
    # Union at top level
    parts = split_top_level_union(text)
    if len(parts) > 1:
        out: list[str] = []
        for p in parts:
            out.extend(flatten_pyright_type(p))
        return out
    # Literal[42] → int;  Literal["x"] → str
    lit = _LITERAL_RE.match(text)
    if lit:
        inner = lit.group(1).strip()
        if inner in _LITERAL_VALUE_TO_TYPE:
            return [_LITERAL_VALUE_TO_TYPE[inner]]
        if "," in inner:
            out: list[str] = []
            for item in split_literal_items(inner):
                out.extend(literal_item_type(item))
            return sorted(set(out))
        return literal_item_type(inner)
    # list[int] / dict[str, int] → flat name
    gen = _GENERIC_RE.match(text)
    if gen:
        return [normalize_class_name(gen.group(1))]
    return [normalize_class_name(text)]


def _strip_outer_parens(text: str) -> str:
    """If ``text`` starts with ``(`` and ends with a balanced ``)`` that wraps
    the whole expression, return the inner text. Otherwise unchanged."""
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        balanced = True
        for i, ch in enumerate(text):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i < len(text) - 1:
                    balanced = False
                    break
        if balanced:
            text = text[1:-1].strip()
        else:
            break
    return text


def _looks_like_callable(text: str) -> bool:
    """Top-level ` -> ` => callable. Inner `() -> Unknown` inside
    `dict[str, () -> Unknown]` stays a dict."""
    if " -> " not in text:
        return False
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if depth == 0 and text.startswith(" -> ", i):
            head = text[:i]
            return head.endswith(")") and "(" in head
        i += 1
    return False


def split_top_level_union(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in text:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "|" and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def split_literal_items(inner: str) -> list[str]:
    out: list[str] = []
    depth = 0
    buf: list[str] = []
    in_str = False
    quote = ""
    for ch in inner:
        if in_str:
            buf.append(ch)
            if ch == quote and (len(buf) < 2 or buf[-2] != "\\"):
                in_str = False
            continue
        if ch in ("'", '"'):
            in_str = True
            quote = ch
            buf.append(ch)
            continue
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return out


def literal_item_type(item: str) -> list[str]:
    item = item.strip()
    if item in _LITERAL_VALUE_TO_TYPE:
        return [_LITERAL_VALUE_TO_TYPE[item]]
    if item.startswith(("'", '"')):
        return ["str"]
    if item.startswith("b'") or item.startswith('b"'):
        return ["bytes"]
    try:
        int(item)
        return ["int"]
    except ValueError:
        pass
    try:
        float(item)
        return ["float"]
    except ValueError:
        pass
    return []


def normalize_class_name(name: str) -> str:
    """Map a class-name string onto TypeEvalPy's flat vocabulary.

    Built-in types are emitted lowercase (`int`, `str`, ...). Stdlib types
    and user-class names from other modules KEEP their module qualifier —
    TypeEvalPy GT for these uses the qualified form (`itertools.groupby`,
    `to_import.A`, etc.), so stripping the prefix produces a false miss.
    `builtins.` is the one prefix we always strip (some checkers emit
    `builtins.int` where TypeEvalPy expects bare `int`).
    """
    if name.startswith("builtins."):
        name = name[len("builtins."):]
    # Snippets are always copied into /tmp/<tool>_work/snippet/main.py, so
    # same-file class names come back as main.<X>. Strip that prefix —
    # GT uses the bare name for in-module references and a real module
    # prefix only for cross-module imports.
    elif name.startswith("main."):
        name = name[len("main."):]
    bare = name.rsplit(".", 1)[-1]
    low = bare.lower()
    if low in {"int", "str", "float", "bool", "bytes", "complex",
              "list", "dict", "tuple", "set", "frozenset"}:
        return low
    if low in {"none", "nonetype"}:
        return "Nonetype"
    return name


if __name__ == "__main__":
    if not utils.is_running_in_docker():
        print("not running in docker — refusing to run on host")
        sys.exit(1)
    parser = argparse.ArgumentParser()
    parser.add_argument("--bechmark_path", default="/tmp/micro-benchmark")
    args = parser.parse_args()
    sys.exit(main_runner(args.bechmark_path))
