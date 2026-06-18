#!/usr/bin/env python3
"""
CLI: audit / fix TypeEvalPy-autogen ground-truth (line, col) positions across a
whole corpus (or the templates), using the canonical re-derivation core in
``autogen/gt_positions.py`` as the single source of truth.

  --audit (default) : report, per file and corpus-wide, how many GT facts
                      disagree with the real AST target position (no writes).
  --fix             : rewrite every flat ``*_gt.json`` in place so each fact's
                      position equals the AST truth. Idempotent.

Facts whose position already matches the AST are left untouched; facts that
cannot be mapped to any AST node (corpus name/type corruption, exec-defined
variables) are reported as "unmatched" and left untouched.

Run from the TypeEvalPy repo root, e.g.:
    python experiments/autogen-gt-fix/rederive_gt_positions.py autogen_typeevalpy_benchmark --fix
    python experiments/autogen-gt-fix/rederive_gt_positions.py micro-benchmark-autogen-templates   # audit templates
"""

import argparse
import json
import sys
from pathlib import Path

# Import the canonical core from autogen/gt_positions.py (single source of truth,
# also wired into the generator's save_files).
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "autogen"))
from gt_positions import Index, resolve_facts  # noqa: E402


# ----------------------------------------------------------------------------
def process_corpus(root, fix=False):
    gt_files = sorted(Path(root).rglob("*_gt.json"))
    stats = {
        "files": 0,
        "facts": 0,
        "already_correct": 0,
        "corrected": 0,
        "unmatched": 0,
        "files_changed": 0,
    }
    diffs = []       # (file, fact-desc, old, new)
    unmatched = []   # (file, fact)
    for gt_path in gt_files:
        py_path = gt_path.with_name(gt_path.name.replace("_gt.json", ".py"))
        if not py_path.exists():
            unmatched.append((str(gt_path), {"_error": "no sibling .py"}))
            continue
        try:
            index = Index(py_path.read_text())
        except SyntaxError as e:
            unmatched.append((str(gt_path), {"_error": f"syntax: {e}"}))
            continue

        raw = json.loads(gt_path.read_text())
        # Corpus files are a flat list; template files wrap facts in
        # {"replacement_mode":..., "ground_truth":[...]}. Audit either shape;
        # only the flat corpus shape is ever rewritten by --fix.
        is_template = isinstance(raw, dict)
        data = raw["ground_truth"] if is_template else raw
        stats["files"] += 1
        changed = False
        resolved = resolve_facts(data, index)
        for fact, new in zip(data, resolved):
            stats["facts"] += 1
            if new is None:
                stats["unmatched"] += 1
                unmatched.append((str(gt_path), dict(fact)))
                continue
            new_line, new_col = new
            if fact.get("line_number") == new_line and fact.get("col_offset") == new_col:
                stats["already_correct"] += 1
                continue
            stats["corrected"] += 1
            diffs.append(
                (
                    str(gt_path),
                    _fact_desc(fact),
                    (fact.get("line_number"), fact.get("col_offset")),
                    (new_line, new_col),
                )
            )
            if fix:
                fact["line_number"] = new_line
                fact["col_offset"] = new_col
                changed = True

        if fix and changed and not is_template:
            gt_path.write_text(json.dumps(data, indent=4))
            stats["files_changed"] += 1

    return stats, diffs, unmatched


def _fact_desc(fact):
    for k in ("function", "parameter", "variable"):
        if k in fact:
            tag = fact[k]
            if "parameter" in fact:
                tag = f"{fact.get('function','')}::{fact['parameter']}"
            return f"{k}={tag}"
    return "?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="corpus root (dir containing python_features)")
    ap.add_argument("--fix", action="store_true", help="rewrite *_gt.json in place")
    ap.add_argument("--dump", help="write full diff/unmatched detail to this json")
    ap.add_argument("--show", type=int, default=20, help="sample rows to print")
    args = ap.parse_args()

    stats, diffs, unmatched = process_corpus(args.root, fix=args.fix)

    print("== summary ==")
    for k, v in stats.items():
        print(f"  {k:18} {v}")
    print(f"  mismatches (GT != AST): {len(diffs)}")
    print(f"  unmatched facts       : {len(unmatched)}")

    if diffs:
        print(f"\n== sample mismatches (first {args.show}) ==")
        for f, desc, old, new in diffs[: args.show]:
            short = f.split("python_features/")[-1]
            print(f"  {old} -> {new}  {desc}  [{short}]")
    if unmatched:
        print(f"\n== sample UNMATCHED (first {args.show}) — investigate! ==")
        for f, fact in unmatched[: args.show]:
            short = f.split("python_features/")[-1]
            print(f"  {short}  {fact}")

    if args.dump:
        Path(args.dump).write_text(
            json.dumps(
                {
                    "stats": stats,
                    "mismatches": [
                        {"file": f, "fact": d, "old": o, "new": n}
                        for f, d, o, n in diffs
                    ],
                    "unmatched": [{"file": f, "fact": fc} for f, fc in unmatched],
                },
                indent=2,
            )
        )
        print(f"\nwrote detail -> {args.dump}")

    # exit non-zero if any fact could not be mapped (so CI/audit notices)
    return 1 if unmatched else 0




if __name__ == "__main__":
    sys.exit(main())
