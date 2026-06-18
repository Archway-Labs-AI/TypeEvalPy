#!/usr/bin/env python3
"""Re-derive autogen ground-truth SHAPE (record kind-key + declared type) from the
AST / CPython truth of the generated main.py, fixing two stale-corpus GT-shape bugs
that the committed corpus carries because it was generated (2024-08-04, cede45d7)
from templates that were corrected only later (7990076a, cf218c01) and never
regenerated.

Two narrowly-scoped, independently-verifiable re-derivation rules:

  RULE 1 (record-shape, AST-grounded) -- the `recursive_tuple` `a`@33 bug.
    A GT record that maps to adapter kind="return" (carries ONLY a `function` key,
    no `variable`/`parameter`) MUST annotate a real function definition. If instead
    the name is an assignment Store target in the generated AST (not a FunctionDef),
    the record is a mis-keyed variable: re-key `function` -> `variable`. The benchmark
    adapter otherwise routes it through return-type resolution and reports the called
    function's RETURN type, mis-scoring a correctly-emitted `callable` binding.

  RULE 2 (declared-type, CPython-grounded) -- the `import_as` `a`@11 bug.
    A `variable` record whose RHS in the AST is a call to a LOCALLY-DEFINED,
    ZERO-ARGUMENT function (`x = local_func()`, the autogen "value function" shape:
    `def local_func(): return <const>`) must declare the type CPython actually
    produces for that binding. If the declared type disagrees with CPython, re-derive
    from CPython. This catches GT that hardcodes a fixed type (`str`) regardless of
    the local function's return.
    The NULLARY restriction is deliberate: it captures the autogen value-function
    pattern exactly while excluding argument-dependent calls (e.g. the decorator case
    `c = func(True, True)`, where `a + b` promotes bool->int and the SAME root GT bug
    also taints return/parameter/param-fed records this single rule could only
    half-fix -- a separate, out-of-scope cluster left for full regeneration).

Both rules are idempotent and surgical: nothing else in the record (name, position,
ordering, other facts) is touched. Run with no flag to AUDIT (read-only, prints +
optionally writes an audit json); add --fix to rewrite in place.

Usage:
  python3 rederive_gt_shape.py <corpus_root> [--fix] [--audit-out path.json]
"""
import argparse
import ast
import json
import os
import subprocess
import sys

PYNAME_TO_GT = {
    "int": "int", "float": "float", "str": "str", "bool": "bool",
    "list": "list", "dict": "dict", "tuple": "tuple",
    "NoneType": "Nonetype", "function": "callable",
    "builtin_function_or_method": "callable",
}


def adapter_kind(rec):
    if "parameter" in rec and "function" in rec:
        return "parameter"
    if "variable" in rec:
        return "variable"
    if "function" in rec:
        return "return"
    return "unknown"


def build_ast_index(main_py):
    """Return (store_targets, func_defs, local_call_rhs) for the module.
    store_targets: {(line, name)} of assignment Store-target Names.
    func_defs:     {(line, name)} of FunctionDef/AsyncFunctionDef name sites,
                   plus {name} of all locally-defined function names.
    local_call_rhs:{(line, name) -> called_func_name} for `name = func(...)`
                   assignments whose callee is a bare Name."""
    src = open(main_py).read()
    tree = ast.parse(src)
    store_targets, def_sites, nullary_func_names, local_call_rhs = set(), set(), set(), {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            def_sites.add((node.lineno, node.name))
            a = node.args
            n_params = (len(a.posonlyargs) + len(a.args) + len(a.kwonlyargs)
                        + (1 if a.vararg else 0) + (1 if a.kwarg else 0))
            if n_params == 0:
                nullary_func_names.add(node.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            store_names = [
                n.id for t in node.targets for n in ast.walk(t)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
            ]
            for nm in store_names:
                store_targets.add((node.lineno, nm))
            # local-call RHS: a single Name target = Call(func=Name)
            if (isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                local_call_rhs[(node.lineno, node.targets[0].id)] = node.value.func.id
    return store_targets, def_sites, nullary_func_names, local_call_rhs


def cpython_type(case_dir, varname):
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
        out = subprocess.run([sys.executable, "-c", code], cwd=case_dir,
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return None
        return PYNAME_TO_GT.get(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return None


def process_file(gt_path, do_fix):
    case_dir = os.path.dirname(gt_path)
    main_py = os.path.join(case_dir, "main.py")
    if not os.path.exists(main_py):
        return []
    try:
        store_targets, def_sites, nullary_func_names, local_call_rhs = build_ast_index(main_py)
    except SyntaxError:
        return []
    recs = json.load(open(gt_path))
    changes = []
    cpy_cache = {}
    for idx, rec in enumerate(recs):
        if rec.get("file") != "main.py":
            continue
        line = rec.get("line_number")
        kind = adapter_kind(rec)
        # RULE 1: function-only record that is actually an assignment target
        if kind == "return":
            name = rec["function"]
            if (line, name) not in def_sites and (line, name) in store_targets:
                changes.append({
                    "rule": "R1-shape-function->variable", "gt_path": gt_path,
                    "line": line, "name": name,
                    "before": dict(rec),
                })
                if do_fix:
                    newrec = {}
                    for k, v in rec.items():
                        newrec["variable" if k == "function" else k] = v
                    recs[idx] = newrec
            continue
        # RULE 2: variable record bound by a local-function call, type != CPython
        if kind == "variable":
            name = rec["variable"]
            callee = local_call_rhs.get((line, name))
            if callee is not None and callee in nullary_func_names:
                if name not in cpy_cache:
                    cpy_cache[name] = cpython_type(case_dir, name)
                cpy = cpy_cache[name]
                if cpy is not None and rec.get("type") != [cpy]:
                    changes.append({
                        "rule": "R2-type-from-cpython", "gt_path": gt_path,
                        "line": line, "name": name, "callee": callee,
                        "before_type": rec.get("type"), "after_type": [cpy],
                    })
                    if do_fix:
                        recs[idx]["type"] = [cpy]
    if do_fix and changes:
        with open(gt_path, "w") as f:
            json.dump(recs, f, indent=4)
    return changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus_root")
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--audit-out")
    args = ap.parse_args()

    root = os.path.join(args.corpus_root, "python_features")
    gt_files = []
    for dirpath, _, files in os.walk(root):
        for fn in files:
            if fn == "main_gt.json":
                gt_files.append(os.path.join(dirpath, fn))
    gt_files.sort()

    all_changes = []
    for gt in gt_files:
        all_changes.extend(process_file(gt, args.fix))

    from collections import Counter
    by_rule = Counter(c["rule"] for c in all_changes)
    by_family = Counter(
        os.path.basename(os.path.dirname(c["gt_path"])).rsplit("_", 1)[0]
        if "_" in os.path.basename(os.path.dirname(c["gt_path"])) else
        os.path.basename(os.path.dirname(c["gt_path"]))
        for c in all_changes
    )
    print(f"scanned {len(gt_files)} files; {'APPLIED' if args.fix else 'would change'} "
          f"{len(all_changes)} records")
    for r, n in sorted(by_rule.items()):
        print(f"  {n:5d}  {r}")
    print("  by case-family (top 12):")
    for fam, n in by_family.most_common(12):
        print(f"    {n:5d}  {fam}")

    if args.audit_out:
        json.dump(all_changes, open(args.audit_out, "w"), indent=2)
        print(f"wrote {args.audit_out}")


if __name__ == "__main__":
    main()
