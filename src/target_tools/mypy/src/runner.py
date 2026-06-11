"""mypy runner — reveal_type injection -> `mypy --check-untyped-defs` -> main_result.json.

Per snippet:
  1. Parse main.py + main_gt.json with `ast`.
  2. Build a transformed copy of main.py that inserts one `reveal_type(...)`
     per GT entry, recording the (inserted_line_number -> gt_index) map.
  3. Run `mypy --check-untyped-defs --no-color-output --no-error-summary
     transformed.py`. The --check-untyped-defs flag tells mypy to walk
     unannotated function bodies (default behavior is to skip them).
  4. Parse stdout for `main.py:LINE: note: Revealed type is "TYPE"` lines.
  5. Flatten mypy's type strings onto TypeEvalPy's flat vocabulary.
  6. Write main_result.json next to main.py.

Mypy is annotation-driven by design — unhinted function returns surface as
`Any`. That's expected; the score reflects what mypy actually does on
TypeEvalPy's deliberately-unhinted corpus.
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

logger = logging.getLogger("mypy-runner")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logger.addHandler(handler)

MYPY_BIN = "mypy"

# mypy output shape:
#   main.py:5: note: Revealed type is "int"
REVEAL_RE = re.compile(r'^[^:]+:(\d+):\s*note:\s*Revealed type is\s+"(.*)"\s*$')


@dataclass
class Insertion:
    after_line: int
    indent: str
    expr: str
    gt_index: int


@dataclass
class FnInfo:
    name: str
    params: dict[str, ast.arg] = field(default_factory=dict)
    body_first_line: int = -1
    body_indent: str = ""
    returns: list[ast.Return] = field(default_factory=list)


def main_runner(benchmark_path: str) -> int:
    root = Path(benchmark_path).resolve()
    gt_files = sorted(root.rglob("main_gt.json"))
    logger.info(f"mypy runner sweeping {len(gt_files)} snippets under {root}")
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

    work_root = Path(tempfile.mkdtemp(prefix="mypy_work_"))
    try:
        shutil.copytree(snippet_dir, work_root / "snippet", dirs_exist_ok=True)
        work_main = work_root / "snippet" / "main.py"
        work_main.write_text(transformed)
        mypy_output = run_mypy(work_main)
    finally:
        shutil.rmtree(work_root, ignore_errors=True)

    predictions = parse_mypy_output(mypy_output, line_to_gt, gt, synthetic)
    (snippet_dir / "main_result.json").write_text(json.dumps(predictions, indent=2) + "\n")


def collect_functions(tree: ast.Module) -> dict[str, FnInfo]:
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


def _resolve_variable_position(
    tree: ast.Module, line: int, name: str, source: str
) -> tuple[int, str]:
    """Find where to insert a reveal_type for a variable defined at GT line.

    GT reports the first line a variable's binding statement begins on; for
    multi-line list/dict/call assignments and for-loop binders, that isn't a
    safe place to inject `reveal_type(<name>)`. See the pyright runner's copy
    of this helper for the full discussion; we keep parallel implementations
    so each tool's runner stays standalone.
    """
    default = (line, leading_indent(source, line))
    for node in ast.walk(tree):
        node_line = getattr(node, "lineno", None)
        if node_line != line:
            continue
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            end = getattr(node, "end_lineno", None)
            if end and end > line:
                return end, leading_indent(source, line)
        if isinstance(node, (ast.For, ast.AsyncFor)) and _target_binds_name(node.target, name):
            if node.body:
                first = node.body[0]
                return first.lineno - 1, " " * first.col_offset
        if isinstance(node, ast.With) and node.body:
            for item in node.items:
                if item.optional_vars is not None and _target_binds_name(item.optional_vars, name):
                    first = node.body[0]
                    return first.lineno - 1, " " * first.col_offset
    return default


def _target_binds_name(target: ast.AST, name: str) -> bool:
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
            return [Insertion(after_line=-1, indent="", expr="None", gt_index=gt_index)]
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
    lines = source.splitlines()
    by_line: dict[int, list[Insertion]] = {}
    synthetic: dict[int, list[str]] = {}
    for ins in insertions:
        if ins.after_line < 0:
            synthetic.setdefault(ins.gt_index, []).append(
                "Nonetype" if ins.expr == "None" else ins.expr
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


def run_mypy(path: Path) -> str:
    """Run mypy on a single file. Returns combined stdout (which carries the
    note lines). Mypy exits non-zero when issues fire, so we ignore exit code."""
    proc = subprocess.run(
        [MYPY_BIN, "--check-untyped-defs", "--no-color-output",
         "--no-error-summary", "--show-traceback", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def parse_mypy_output(
    mypy_output: str,
    line_to_gt: dict[int, int],
    gt: list[dict],
    synthetic: dict[int, list[str]],
) -> list[dict]:
    revealed: dict[int, list[str]] = {}
    for raw in mypy_output.splitlines():
        m = REVEAL_RE.match(raw.strip())
        if not m:
            continue
        line_no = int(m.group(1))
        gt_index = line_to_gt.get(line_no)
        if gt_index is None:
            continue
        types = flatten_mypy_type(m.group(2))
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
# Map mypy type strings -> TypeEvalPy flat vocabulary.
# Mypy uses concise type names (no literal narrowing):
#   `int`, `str`, `bool`, `list[int]`, `dict[str, int]`, `tuple[int, str]`,
#   `int | str`, `Any`, `None`, `def () -> int` (callable signatures),
#   class names by their fully-qualified path (`main.MyClass` etc.).
# ---------------------------------------------------------------------------

_LITERAL_RE = re.compile(r"^Literal\[(.*)\]$")
_GENERIC_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\[")
_LITERAL_VALUE_TO_TYPE = {
    "True": "bool",
    "False": "bool",
    "None": "Nonetype",
}


def flatten_mypy_type(text: str) -> list[str]:
    text = text.strip()
    # mypy sometimes appends a trailing `?` to Literal types meaning the
    # literal is tentative — strip it; flat vocabulary doesn't distinguish.
    if text.endswith("?"):
        text = text[:-1].strip()
    if not text or text in ("Any", "Never", "<nothing>"):
        return []
    if text == "None":
        return ["Nonetype"]
    if text.startswith("def "):
        return ["callable"]
    if text.startswith("Type[") and text.endswith("]"):
        return ["type"]
    # Union at top level
    parts = split_top_level_union(text)
    if len(parts) > 1:
        out: list[str] = []
        for p in parts:
            out.extend(flatten_mypy_type(p))
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
    # list[int] / dict[str, int] -> flat name
    gen = _GENERIC_RE.match(text)
    if gen:
        return [normalize_class_name(gen.group(1))]
    return [normalize_class_name(text)]


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


def normalize_class_name(name: str) -> str:
    """Strip module prefix and map to TypeEvalPy's lowercase builtins."""
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    low = name.lower()
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
