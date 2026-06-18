#!/usr/bin/env python3
"""Sample run-34 TYPE_MISS cases for the two GT-shape clusters and classify each
as GT-SHAPE BUG (corpus GT wrong vs CPython truth) or GENUINE (engine wrong).

For each sampled case we gather FOUR independent sources of truth at the missed
binding and cross-check them:

  GT       : the autogen ground-truth record  -> declared type + record SHAPE (which
             kind-key it carries; mirrors typeevalpy_mapping.from_record).
  AST      : what the generated main.py actually binds (ast.parse): is the name a
             `def` (return annotation) or an assignment Store target (variable)?
  CPython  : what CPython actually produces at that binding (subprocess oracle).
  ENGINE   : the engine's RAW binding type (optional direct replay via the engine
             worktree's analysis_server) -- NOT the adapter's downstream resolution.

The adapter-routing subtlety (the assignments cluster): when a GT record is keyed
`function` but the AST node is an assignment target, the benchmark adapter routes it
through return-type resolution, so the `predicted_types` recorded in runs.db is
func1's RETURN type, NOT the engine's emitted binding (which is `callable`). We must
therefore classify on (AST truth + GT-declared-type vs CPython + engine RAW binding),
not on the recorded predicted_types alone.

Usage:
  python3 sample_and_classify.py <corpus_root> <runs.db> <out.json> [N] [engine_src_dir]
"""
import ast
import json
import os
import subprocess
import sys
from collections import Counter

# CPython runtime type name -> TypeEvalPy GT type token
PYNAME_TO_GT = {
    "int": "int", "float": "float", "str": "str", "bool": "bool",
    "list": "list", "dict": "dict", "tuple": "tuple",
    "NoneType": "Nonetype", "function": "callable",
    "builtin_function_or_method": "callable",
}

# engine element -> TypeEvalPy GT type token
_engine_analyze = None


def load_engine(engine_src):
    global _engine_analyze
    if not engine_src:
        return False
    try:
        if engine_src not in sys.path:
            sys.path.insert(0, engine_src)
        from sd_core.analysis_server import analyze_package  # noqa
        _engine_analyze = analyze_package
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  [engine replay unavailable: {e}]", file=sys.stderr)
        return False


def engine_element_to_gt(el):
    if not isinstance(el, dict):
        return str(el)
    kind = el.get("kind")
    if kind == "pytype":
        return PYNAME_TO_GT.get(el.get("name"), el.get("name"))
    if kind == "callable":
        return "callable"
    return kind  # list/dict/tuple/...


def engine_binding_type(case_dir, name):
    if _engine_analyze is None:
        return None
    try:
        res = _engine_analyze(case_dir, "main.py")
        hist = res["module"]["bindings"].get(name, [])
        if not hist:
            return ["<no-binding>"]
        return [engine_element_to_gt(hist[-1]["element"])]
    except Exception as e:  # noqa: BLE001
        return [f"<engine-exc: {e}>"]


def cpython_binding_type(case_dir, varname):
    """Run main.py in CPython (subprocess, cwd=case_dir so local imports resolve)
    and report type(<varname>) after the module finishes executing."""
    code = (
        "import runpy, types\n"
        "ns = runpy.run_path('main.py', run_name='__main__')\n"
        f"v = ns.get({varname!r})\n"
        "tn = type(v).__name__\n"
        "if isinstance(v, (types.FunctionType, types.BuiltinFunctionType)):\n"
        "    tn = 'function'\n"
        "print(tn)\n"
    )
    try:
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=case_dir,
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            tail = out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "rc!=0"
            return f"<cpython-error: {tail}>"
        return PYNAME_TO_GT.get(out.stdout.strip(), out.stdout.strip())
    except Exception as e:  # noqa: BLE001
        return f"<oracle-exc: {e}>"


def ast_assignment_kind(main_py, line, name):
    """Is (line, name) a `def`/lambda (=> a real return annotation) or an
    assignment Store target (=> a variable)? Returns (kind, rhs_shape)."""
    tree = ast.parse(open(main_py).read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno == line and node.name == name:
                return "def", None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and node.lineno == line:
            names = []
            for t in node.targets:
                names.extend(
                    n.id for n in ast.walk(t)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
                )
            if name in names:
                shape = "call" if isinstance(node.value, ast.Call) else (
                    "name-or-tuple" if isinstance(node.value, (ast.Name, ast.Tuple)) else "other")
                return "assign-target", shape
    return "unknown", None


def gt_record_for(gt_path, line, name):
    for r in json.load(open(gt_path)):
        if r.get("line_number") != line:
            continue
        if name in (r.get("variable"), r.get("function"), r.get("parameter")):
            return r
    return None


def gt_record_kind(rec):
    """Mirror typeevalpy_mapping.from_record."""
    if rec is None:
        return None
    if "parameter" in rec and "function" in rec:
        return "parameter"
    if "variable" in rec:
        return "variable"
    if "function" in rec:
        return "return"
    return "unknown"


def classify(adapter_kind, ast_kind, gt_type, cpy, engine_raw):
    """Truth-table classification.
    engine_raw is the engine's RAW binding type (list) or None if unavailable."""
    cpy_list = [cpy]
    gt_ok = (gt_type == cpy_list)
    eng_ok = (engine_raw == cpy_list) if engine_raw is not None else None

    # Record-SHAPE bug: GT routes as a `return` but the AST node is an assignment
    # target -> the adapter mis-resolves; GT's own declared type still == CPython.
    if adapter_kind == "return" and ast_kind == "assign-target":
        if gt_ok and (eng_ok in (True, None)):
            return "GT-SHAPE BUG (record-shape: function->variable)"
        return "NEEDS-REVIEW (return-keyed assign-target, GT type != CPython)"

    # Type-VALUE bug: GT shape is right (variable) but its declared type != CPython,
    # while the engine agrees with CPython.
    if gt_type is not None and not gt_ok:
        if eng_ok is True or (eng_ok is None):
            return "GT-SHAPE BUG (type-value: hardcoded wrong type)"
        return "GENUINE (engine wrong)"

    if gt_ok and eng_ok is False:
        return "GENUINE (engine wrong)"
    if gt_ok and eng_ok is True:
        return "BOTH-MATCH (no bug here)"
    return "UNCLEAR"


def main():
    corpus_root, db, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    n = int(sys.argv[4]) if len(sys.argv) > 4 else 15
    engine_src = sys.argv[5] if len(sys.argv) > 5 else None
    have_engine = load_engine(engine_src)

    import sqlite3
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    results = {"_meta": {"engine_replay": have_engine, "engine_src": engine_src}}
    for cluster, category in (("assignments", "assignments"), ("imports", "imports")):
        rows = con.execute(
            "SELECT DISTINCT suite_path FROM annotations "
            "WHERE run_id=34 AND category=? AND outcome='TYPE_MISS' ORDER BY suite_path",
            (category,)
        ).fetchall()
        paths = [r["suite_path"] for r in rows]
        step = max(1, len(paths) // n)
        sample = paths[::step][:n]

        cluster_rows = []
        for sp in sample:
            anns = con.execute(
                "SELECT line, col, kind, name, expected_types, predicted_types "
                "FROM annotations WHERE run_id=34 AND suite_path=? AND outcome='TYPE_MISS'",
                (sp,)
            ).fetchall()
            case_dir = os.path.join(corpus_root, "python_features", sp)
            main_py = os.path.join(case_dir, "main.py")
            gt_path = os.path.join(case_dir, "main_gt.json")
            for a in anns:
                line, name = a["line"], a["name"]
                rec = gt_record_for(gt_path, line, name)
                adapter_kind = gt_record_kind(rec)
                ast_kind, ast_shape = ast_assignment_kind(main_py, line, name)
                cpy = cpython_binding_type(case_dir, name)
                gt_type = rec.get("type") if rec else None
                pred = json.loads(a["predicted_types"]) if a["predicted_types"] else None
                engine_raw = engine_binding_type(case_dir, name) if have_engine else None
                verdict = classify(adapter_kind, ast_kind, gt_type, cpy, engine_raw)
                cluster_rows.append({
                    "suite_path": sp, "line": line, "name": name,
                    "adapter_kind_from_GT": adapter_kind,
                    "gt_record_keys": [k for k in ("function", "variable", "parameter")
                                       if rec and k in rec],
                    "ast_truth": {"kind": ast_kind, "rhs_shape": ast_shape},
                    "cpython_type": cpy,
                    "gt_declared_type": gt_type,
                    "engine_raw_binding": engine_raw,
                    "adapter_predicted_in_db": pred,
                    "verdict": verdict,
                })
        results[cluster] = cluster_rows

    json.dump(results, open(out_path, "w"), indent=2)
    for cluster in ("assignments", "imports"):
        rows = results[cluster]
        print(f"\n=== {cluster} ({len(rows)} sampled) ===")
        for v, k in Counter(r["verdict"] for r in rows).items():
            print(f"  {k:3d}  {v}")
        for r in rows[:6]:
            print(f"   {r['suite_path'].split('/')[-1][:40]:40s} L{r['line']} {r['name']} | "
                  f"GTkind={r['adapter_kind_from_GT']:8s} GTtype={r['gt_declared_type']} "
                  f"CPy=[{r['cpython_type']}] ENGraw={r['engine_raw_binding']} "
                  f"ADPpred={r['adapter_predicted_in_db']}")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
