#!/usr/bin/env python3
"""Scoped regeneration test: run the ACTUAL generator code path (helpers.save_files,
now wired to gt_positions.rederive_kinds + rederive_facts) on just the two affected
templates, into a temp dir, then verify the produced ground-truth shape is correct.

This proves a FUTURE full regeneration emits the fixed GT (the generator-side half
of the fix), independent of the in-place corpus post-process.

Usage: python3 regen_test.py <repo_root>
"""
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
sys.path.insert(0, os.path.join(REPO, "autogen"))

from helpers import read_template, process_file, process_import_case  # noqa: E402

TEMPLATES = os.path.join(REPO, "micro-benchmark-autogen-templates")


def regen(rel_template_dir, out):
    main_py = Path(TEMPLATES) / rel_template_dir / "main.py"
    td = read_template(main_py)
    file_path = str(main_py.parent).replace(TEMPLATES, "")
    if td["replacement_mode"] == "Imports":
        process_import_case(
            name=td["name"], data_types=td["data_types"],
            code_template=td["code_template"], json_template=td["json_template"],
            file_path=file_path, file_parent=str(main_py.parent), output_folder=out,
        )
    else:
        process_file(
            name=td["name"], data_types=td["data_types"],
            code_template=td["code_template"], json_template=td["json_template"],
            file_path=file_path, output_folder=out,
        )


def check_recursive_tuple(out):
    """Every generated recursive_tuple case must key a@33 as `variable`, never `function`."""
    base = Path(out) / "python_features" / "assignments"
    cases = sorted(base.glob("recursive_tuple_*/main_gt.json"))
    bad_function, ok_variable = 0, 0
    for gt in cases:
        for r in json.load(open(gt)):
            if r.get("line_number") == 33 and (
                    r.get("variable") == "a" or r.get("function") == "a"):
                if "function" in r and "variable" not in r:
                    bad_function += 1
                elif r.get("variable") == "a":
                    ok_variable += 1
    return len(cases), ok_variable, bad_function


def check_import_as(out):
    """Every generated import_as case: a@11 (a = func(), a LOCAL call) must declare
    the same type as the local func's return annotation (the `function: func`
    record @ line 7). Robust to filename conventions; mirrors `a = func()`."""
    base = Path(out) / "python_features" / "imports"
    cases = sorted(base.glob("import_as_*/main_gt.json"))
    correct, wrong, examples = 0, 0, []
    for gt in cases:
        recs = json.load(open(gt))
        func_ret = next((r.get("type") for r in recs
                         if r.get("line_number") == 7 and r.get("function") == "func"), None)
        for r in recs:
            if r.get("line_number") == 11 and r.get("variable") == "a":
                got = r.get("type")
                if got == func_ret:
                    correct += 1
                else:
                    wrong += 1
                    if len(examples) < 5:
                        examples.append((gt.parent.name, got, func_ret))
    return len(cases), correct, wrong, examples


def main():
    out = tempfile.mkdtemp(prefix="regen_test_")
    print(f"regenerating into {out}")
    regen("python_features/assignments/recursive_tuple", out)
    regen("python_features/imports/import_as", out)

    n_rt, ok_v, bad_f = check_recursive_tuple(out)
    print(f"\n[recursive_tuple] {n_rt} cases regenerated")
    print(f"  a@33 keyed variable (correct): {ok_v}")
    print(f"  a@33 keyed function (BUG):     {bad_f}")

    n_ia, ok, wrong, ex = check_import_as(out)
    print(f"\n[import_as] {n_ia} cases regenerated")
    print(f"  a@11 type == local func return (correct): {ok}")
    print(f"  a@11 type wrong:                          {wrong}")
    for folder, got, want in ex:
        print(f"    {folder}: got {got} want {want}")

    ok_all = (bad_f == 0 and wrong == 0 and ok_v == n_rt and ok == n_ia)
    print(f"\nRESULT: {'PASS - generator emits correct GT shape' if ok_all else 'FAIL'}")
    import shutil
    shutil.rmtree(out, ignore_errors=True)
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
