# Autogen benchmark — ground-truth SHAPE fix (destructuring + cross-module import)

**Goal:** `corpus-gt-shape-investigate`. Two batch-3/4 engine diagnoses concluded
the remaining `assignments` (~720) and `imports` (residual) `TYPE_MISS` are **autogen
ground-truth SHAPE artifacts**, not engine bugs. This experiment confirms that
diagnosis from independent angles, finds the root cause, fixes the generator **and**
the committed corpus, and audits before/after — mirroring the position-fix precedent
on this same branch lineage (`9afcfc9b`, see [`../autogen-gt-fix/REPORT.md`](../autogen-gt-fix/REPORT.md)).

**Outcome: RESULT.** Both clusters are **GT-SHAPE BUGS** (corpus GT wrong vs the
CPython truth signal; the engine is correct). Both are the *same* failure mode as the
position fix: the committed corpus (last generated 2024-08-04, `cede45d7`) is **stale
relative to its templates**, which were corrected only *later* (`7990076a` 2025-10-09,
`cf218c01` 2025-11-13) and never regenerated. **756 records** were corrected
(720 record-shape + 36 declared-type); the GT-shape-vs-truth mismatch count went
**756 → 0**, with **0** position edits, **0** record-count changes, and **0** changes
to any previously-correct GT (verified). The fix is applied **both** in the generator
(`autogen/gt_positions.py::rederive_kinds`, wired into `helpers.py::save_files`, so
future regenerations self-correct) **and** as an idempotent post-process over the
shipped corpus.

> **NOTE — required follow-on (NOT run here, per the gate):** converting this GT
> correction into the official autogen number requires a **full autogen re-run vs the
> engine** (~77k facts scored) on the corrected corpus. That is a separate step and
> was intentionally **not** run here. The scoring-recovery evidence below (§4) is a
> targeted, DB-grounded projection, not the full re-score.

---

## 1. The two clusters (run 34 census)

Run 34 = `typeevalpy_autogen`, engine `loop/main 6d7ed3b` (batches 2–4 integrated),
old adapter, apples-to-apples vs run 31. It runs on **this** corpus (the position fix
`9afcfc9b` is already in it — `assignments` LOCATION_MISS is now 0).

| category | EXACT | TYPE_MISS | the miss |
|---|---:|---:|---|
| `assignments` | 32,953 | **720** | `a` @ line 33, all `recursive_tuple_*` |
| `imports` | 2,988 | **36** | `a` @ line 11, all `import_as_*` |

Both clusters are a *single annotation replicated one-per-file*, exactly matching the
prior diagnoses ([`ENGINE-destructuring-unpack.md`](../../../Archway-worktrees/engine-work/docs/design_notes/ENGINE-destructuring-unpack.md),
[`ENGINE-import-crossmodule-type.md`](../../../Archway-worktrees/engine-work/docs/design_notes/ENGINE-import-crossmodule-type.md)).

---

## 2. Per-cluster classification (12–15 sampled cases each; full data in [`classification.json`](classification.json))

Every sampled case is cross-checked against **four** independent sources: the GT
record (declared type + which kind-key it carries), the generated `main.py` **AST**,
the **CPython** subprocess oracle, and the engine's **raw binding** (direct replay via
the engine worktree's `analysis_server` — *not* the adapter's downstream resolution).

### Cluster A — `assignments` / `recursive_tuple` (15/15 → **GT-SHAPE BUG**, record-shape)

The corpus file (verbatim, the relevant lines):

```python
a, (b, c) = func1, (func2, func3)               # line 28  -> a keyed `variable`, EXACT
i = a()                                          # line 29  (a is CALLED here)
...
a, (b, (c, d)) = func1, (func2, (func3, func4))  # line 33  -> a keyed `function`, TYPE_MISS
```

| source | value for `a` @ 33 | verdict |
|---|---|---|
| GT record keys | `["function"]` (no `variable`) → adapter routes as **kind="return"** | the bug |
| GT declared type | `["callable"]` | correct vs CPython |
| AST truth | `a` is an **assignment Store target** (tuple-unpack), **not** a `def` | mis-keyed |
| CPython | `a` is bound to `func1` → **callable** | — |
| engine raw binding | `callable @ row 33` (direct replay) | engine correct |
| adapter `predicted` in DB | `["int"]`/`["float"]`/… = **func1's RETURN type** | adapter mis-resolution |

The benchmark adapter (`typeevalpy_mapping.from_record`) maps a record with only a
`function` key to `kind="return"`, and `archway_adapter` then resolves the called
function's return type. So the GT's *own* declared type (`callable`) is **correct**,
but its *record shape* (`function` key) makes the adapter report `func1`'s return type
→ a spurious `callable ≠ int` miss. **Dispositive evidence the engine is right:** the
**identical** `a` on line 28 (keyed `variable`) scores **EXACT with predicted
`["callable"]` in all 720 files**, and the line-33 siblings `b`/`c`/`d` (keyed
`variable`) are all EXACT. Same `a = func1` binding both lines; only the key differs.

### Cluster B — `imports` / `import_as` (15/15 → **GT-SHAPE BUG**, declared-type)

```python
import to_import as as_to_import
def func(): return 90          # local func (returns the per-case value)
a = func()                     # line 11  -> a = LOCAL call
b = as_to_import.func()        # line 12  -> a = cross-module call
```

| source | value for `a` @ 11 | verdict |
|---|---|---|
| GT declared type | `["str"]` — **hardcoded**, every case | the bug |
| AST truth | `a = func()` where `func` is a **local nullary** def | — |
| CPython | type of local `func`'s return (int/float/bool/list/dict/tuple) | — |
| engine raw binding | same as CPython (direct replay) | engine correct |
| adapter `predicted` in DB | same as CPython | engine correct |

The GT hardcodes `a = str` regardless of what the local `func` returns. CPython and the
engine agree on the real local-call type; the GT is simply wrong. (The 6 cases where the
local func *does* return `str` already score EXACT — hence 42 `import_as` cases but only
**36** misses.) `a` is not even cross-module — the actually-imported name `b` is predicted
correctly.

**No GENUINE engine cases found in either cluster.** Both are corpus GT-shape bugs.

---

## 3. Verified root cause — stale corpus, corrected templates (same as the position fix)

The generator (`helpers.py::replace_placeholders_and_generate_json`) copies each GT
record's **kind-key verbatim** from the template and substitutes only `<valueN>` **type**
placeholders (a literal type like `"str"` passes through untouched). So a template whose
shape is correct yields a correct corpus — and a *stale* template yields a *stale* corpus.

Both templates were fixed **after** the corpus was last generated (`cede45d7`, 2024-08-04):

| cluster | template fix commit | what it changed | corpus (stale) |
|---|---|---|---|
| `recursive_tuple` | `7990076a` (2025-10-09) | line-33 `a`: `"function"` → `"variable"` | still `"function"` |
| `import_as` | `cf218c01` (2025-11-13) | line-11 `a` type: `"str"` → `"<value1>"` | still `"str"` |

`git show 7990076a` / `git show cf218c01` show exactly these template edits; the corpus
never inherited them. (The position-fix `9afcfc9b` corrected `(line, col)` but, by design,
not the record kind-key or the declared type.) This is the precedent's §2.1 root cause
verbatim: *"the committed corpus is stale relative to its templates."*

---

## 4. The fix — where & how

Two narrowly-scoped, independently-verifiable re-derivation rules, each grounded in the
generated `main.py` truth (AST / CPython), never in the template:

**RULE 1 — record-shape, AST-grounded** (fixes `recursive_tuple`, 720). A GT record that
maps to adapter `kind="return"` (carries ONLY a `function` key) MUST annotate a real
function *definition*. If the AST shows the name is an assignment Store target (not a
`def`), re-key `function` → `variable`. New module function
[`autogen/gt_positions.py::rederive_kinds`](../../autogen/gt_positions.py); wired into
`helpers.py::save_files` (runs before `rederive_facts`).

**RULE 2 — declared-type, CPython-grounded** (fixes `import_as`, 36). A `variable` record
whose RHS is a call to a **local nullary** function (`x = local_func()`, the autogen
value-function shape) must declare the type CPython actually produces; correct mismatches
from the CPython oracle. The nullary restriction is deliberate — see §6.

* **Generator (future regenerations).** RULE 1 is added to the generator. RULE 2 needs no
  generator change: the `import_as` template is *already* corrected (`<value1>`) and the
  generator propagates it faithfully. Both proven by a scoped regeneration of the two
  templates through the real generator path ([`regen_test.py`](regen_test.py)):
  `recursive_tuple` **720/720** now key `a@33` as `variable` (0 as `function`); `import_as`
  **42/42** now declare `a@11` == the local func's return type. **RESULT: PASS.**
* **Committed corpus (corrected now).** Both rules drive an idempotent post-process,
  [`rederive_gt_shape.py`](rederive_gt_shape.py) (`--fix`). A full regeneration was
  deliberately avoided (it rewrites every `main.py` with new random values / reformatting /
  renamed folders → a massive unreviewable diff, per precedent §3); the in-place
  post-process is surgical and directly verifiable.

---

## 5. Before / after audit (full data: [`audit_before.json`](audit_before.json), [`audit_after.json`](audit_after.json); summary: [`validation_output.txt`](validation_output.txt))

| | records |
|---|---:|
| corpus files scanned | 5,453 |
| **GT-shape-vs-truth mismatches BEFORE** | **756** |
| &nbsp;&nbsp;RULE 1 (`function`→`variable`, all `recursive_tuple` `a`@33) | 720 |
| &nbsp;&nbsp;RULE 2 (`str`→CPython type, all `import_as` `a`@11) | 36 |
| **GT-shape-vs-truth mismatches AFTER** | **0** |

**Surgical diff (git):** 758 files changed = 720 + 36 corpus + 2 source. Corpus diff is
**exactly** `720 × ("function": "a" → "variable": "a")` and `36 × ("str" → correct type)`;
**0** `line_number`/`col_offset` edits; record counts unchanged per file (e.g. 32→32, 3→3).

**No previously-correct GT broken:** the AFTER re-audit is 0 (nothing left, nothing
over-applied); `rederive_kinds` run against the **142 corrected templates** makes **0**
changes (the convention matches template-author intent — it only fires on the stale
corpus). The 6 `import_as` `str`-returning cases were correctly left untouched.

**Scoring-recovery projection (NOT the full re-score):**
* `imports`: **36/36** corrected GT now EQUAL the engine's recorded run-34 prediction → would score EXACT.
* `assignments`: the line-28 twin (variable-keyed, identical `a=func1` binding) is EXACT with predicted `["callable"]` in **720/720** files, and direct engine replay confirms `a@row33 = callable` in **30/30** sampled files. The corrected record is now variable-keyed too → adapter compares the engine's `callable` binding == GT `callable` → EXACT.

Net projected recovery on a full re-run: **756 TYPE_MISS → EXACT** (720 `assignments` + 36 `imports`), engine and corpus untouched elsewhere.

---

## 6. Out-of-scope finding (discovered, deliberately NOT fixed here)

RULE 2's broader form (any local-function-call RHS, before the nullary restriction) also
surfaced **`decorators/assigned_1_4_bool`**: `c = func(True, True)` where `func` is
decorated and computes `a + b` → `True + True == 2` → **int**, but the GT declares
`bool`. CPython and the engine agree on `int`; the GT is wrong. This is a **genuine
GT bug**, but a *different cluster*: the same `True+True` root error taints **4** records
in that file (`wrapper`@5 return, `result`@6 variable, `func`@16 return, `c`@20 variable),
and the narrow rules here can only re-derive 1 of the 4 (the local-call variable) — a
*partial* fix would corrupt the file. It is therefore **excluded** (nullary restriction)
and left for the full regeneration, which resolves it holistically. Run-34 shows ~5
`["bool"]→["int"]` decorators records of this shape. (The other decorators TYPE_MISS —
`MyClass→type`, `NewClass` — are an unrelated class-annotation issue.) This mirrors the
position fix's §5 out-of-scope section: surface it, don't half-fix it.

---

## 7. Reproduce

```bash
# from the TypeEvalPy repo root (this worktree)

# 1. classify the two clusters from run 34 (GT vs AST vs CPython vs engine raw)
python3 experiments/corpus-gt-shape/sample_and_classify.py \
    autogen_typeevalpy_benchmark /path/to/archway-benchmarks/runs.db \
    experiments/corpus-gt-shape/classification.json 15 \
    /path/to/Archway-worktrees/engine-work        # engine root optional

# 2. audit the committed corpus (read-only): BEFORE reports 756; AFTER reports 0
python3 experiments/corpus-gt-shape/rederive_gt_shape.py autogen_typeevalpy_benchmark \
    --audit-out experiments/corpus-gt-shape/audit_before.json

# 3. apply the fix in place (idempotent)
python3 experiments/corpus-gt-shape/rederive_gt_shape.py autogen_typeevalpy_benchmark --fix

# 4. prove the GENERATOR emits correct GT for future regenerations (scoped)
python3 experiments/corpus-gt-shape/regen_test.py .

# 5. before/after + scoring-recovery evidence
python3 experiments/corpus-gt-shape/validate.py \
    autogen_typeevalpy_benchmark /path/to/archway-benchmarks/runs.db \
    experiments/corpus-gt-shape/audit_before.json \
    experiments/corpus-gt-shape/audit_after.json \
    /path/to/Archway-worktrees/engine-work 30
```

## 8. Artifacts

| file | what |
|---|---|
| `autogen/gt_positions.py` | + `rederive_kinds` — canonical AST record-shape re-derivation |
| `autogen/helpers.py` | `save_files` now calls `rederive_kinds` then `rederive_facts` |
| `sample_and_classify.py` | per-cluster classifier (GT vs AST vs CPython vs engine raw) |
| `classification.json` | full classification of the 15+15 sampled cases (4 sources each) |
| `rederive_gt_shape.py` | corpus audit / `--fix` post-process (both rules) |
| `regen_test.py` | scoped regeneration through the real generator path (PASS) |
| `validate.py` | before/after + scoring-recovery evidence driver |
| `audit_before.json` / `audit_after.json` | full record-level audit (756 → 0) |
| `validation_output.txt` | captured before/after + recovery summary |
