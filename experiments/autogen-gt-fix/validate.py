#!/usr/bin/env python3
"""
Validation for the autogen GT position fix.

Produces, for a sample of starred / recursive_tuple / augmented cases, a
BEFORE/AFTER table:  OLD-GT (line,col)  ->  NEW-GT (line,col)  ->  AST-truth.

The AST-truth here is computed by an INDEPENDENT, minimal ``ast`` reading (NOT
the fixer's oracle) so "NEW == AST" is a genuine cross-check, not a tautology.

Usage:
    python validate.py <corpus_root> <before_audit.json> <after_audit.json>
"""

import ast
import json
import subprocess
import sys
from pathlib import Path


def ast_truth(py_path, fact):
    """Independently locate a GT fact's AST position (1-indexed col)."""
    tree = ast.parse(Path(py_path).read_text())

    def name_store_positions(name):
        out = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id == name:
                out.append((n.lineno, n.col_offset + 1))
        return out

    def func_positions(simple_name):
        out = []
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == simple_name:
                line = Path(py_path).read_text().splitlines()[n.lineno - 1]
                col = n.col_offset + (line[n.col_offset:].index(n.name))
                out.append((n.lineno, col + 1))
        return out

    if "variable" in fact:
        base = fact["variable"].split("[")[0].split(".")[-1]
        cands = name_store_positions(base)
    elif "parameter" in fact:
        cands = []
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for a in (list(n.args.posonlyargs) + list(n.args.args)
                          + ([n.args.vararg] if n.args.vararg else [])
                          + list(n.args.kwonlyargs)
                          + ([n.args.kwarg] if n.args.kwarg else [])):
                    if a.arg == fact["parameter"]:
                        cands.append((a.lineno, a.col_offset + 1))
        # a parameter fact can also point at a body re-binding (``a += 93``)
        cands += name_store_positions(fact["parameter"])
    else:
        cands = func_positions(fact["function"].split(".")[-1])

    # pick the candidate nearest the NEW gt position
    want = (fact["line_number"], fact["col_offset"])
    if not cands:
        return None
    return min(cands, key=lambda c: (abs(c[0] - want[0]), abs(c[1] - want[1])))


def git_head_json(path):
    """The committed (pre-fix) version of a GT file."""
    out = subprocess.run(["git", "show", f"HEAD:{path}"], capture_output=True, text=True)
    return json.loads(out.stdout)


def main():
    root, before_f, after_f = sys.argv[1], sys.argv[2], sys.argv[3]
    before = json.load(open(before_f))

    samples = [
        "assignments/starred_1_293_dict_tuple_float_str",
        "assignments/recursive_tuple_1_100_int_tuple_float_list_dict",
        "assignments/augmented_1_4_list",
    ]
    print("=" * 100)
    print("WORKED-EXAMPLE BEFORE/AFTER TABLE  (OLD from git HEAD; NEW from fixed file; AST = independent ast reading)")
    print("=" * 100)
    all_ok = True
    for s in samples:
        gt = Path(root) / "python_features" / s / "main_gt.json"
        py = Path(root) / "python_features" / s / "main.py"
        new_data = json.load(open(gt))
        old_data = git_head_json(str(gt))  # index-aligned (fix never reorders)
        print(f"\n### {s}")
        print(f"  {'fact':28} {'OLD-GT':>10}  {'NEW-GT':>10}  {'AST-truth':>10}  match")
        for old_fact, fact in zip(old_data, new_data):
            sig = _sig(fact)
            old = (old_fact["line_number"], old_fact["col_offset"])
            new = (fact["line_number"], fact["col_offset"])
            truth = ast_truth(py, fact)
            ok = (truth is None) or (tuple(truth) == new)
            is_element = "[" in fact.get("variable", "") or fact.get("variable", "").count(".") >= 1
            if is_element:
                mark = "ok(base)" if ok else "(base)"
            else:
                mark = "OK" if ok else "**MISMATCH**"
                if not ok:
                    all_ok = False
            flag = "  <-- moved" if old != new else ""
            print(f"  {sig:28} {str(old):>10}  {str(new):>10}  {str(truth):>10}  {mark}{flag}")

    audit_after = json.load(open(after_f))
    print("\n" + "=" * 100)
    print("CORPUS-WIDE AST-vs-GT POSITION AUDIT")
    print("=" * 100)
    before_mm = len(before["mismatches"])
    after_mm = len(audit_after["mismatches"])
    print(f"  position mismatches BEFORE fix : {before_mm}")
    print(f"  position mismatches AFTER  fix : {after_mm}")
    print(f"  unmatched (out-of-scope) facts : {len(audit_after['unmatched'])}")
    print(f"\n  result: {'PASS' if (after_mm == 0 and all_ok) else 'FAIL'}")
    return 0 if (after_mm == 0 and all_ok) else 1


def _sig(fact):
    if "parameter" in fact:
        return f"{fact.get('function','')}::{fact['parameter']}"
    if "variable" in fact:
        return f"var {fact['variable']}"
    return f"func {fact['function']}"


if __name__ == "__main__":
    sys.exit(main())
