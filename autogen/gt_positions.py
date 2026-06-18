#!/usr/bin/env python3
"""
Canonical re-derivation of TypeEvalPy-autogen ground-truth (line, col) positions
from a module's actual AST.

WHY THIS EXISTS
---------------
The autogen pipeline (``generate_typeevalpy_dataset.py`` + ``helpers.py``) builds
each ``*_gt.json`` by copying ``line_number``/``col_offset`` verbatim from the
micro-benchmark *template* ``*_gt.json``. Placeholder substitution never moves a
binding's line/column, so this is usually fine — but it means the GT positions
are only ever as correct as the template's, and they are NOT re-checked against
the file that actually ships. When a template's positions are stale or
inconsistent (e.g. the ``starred`` template once placed the unpacking assignment
one line too low), every generated case inherits the wrong position and is
mis-scored as a LOCATION_MISS even though the engine emitted the right type at
the right place.

This module makes the AST of the generated ``main.py`` the single source of
truth for positions:

  * ``rederive_facts(source, facts)`` — used by the generator (``save_files``) to
    fix positions at generation time.
  * ``Index`` / ``resolve_facts`` — the reusable core (also driven by the
    corpus-wide post-process / audit CLI under ``experiments/autogen-gt-fix``).

POSITION CONVENTION (reproduced from the corpus; validated against the templates
with zero mismatches over 794 matchable facts):

  * 1-indexed columns:  ``GT.col_offset == ast_node.col_offset + 1``
  * function fact     -> the function NAME column on the ``def`` line. The corpus
                         is inconsistent about qualification, so a def is indexed
                         under its bare name, its class-qualified path, AND its
                         full (class+function) path; the GT string selects one.
  * parameter fact    -> the ``arg`` node, or a body re-binding of the parameter
                         (``a += 93``), or — when the name is dotted — a
                         mislabeled ``self`` attribute.
  * lambda fact       -> the lambda's parameter ``arg`` node.
  * variable (plain)  -> the assignment-target ``Name`` (Store ctx).
  * variable (subscript) -> the explicit subscript target (``d["b"] = ...``) when
                         one exists, else the base binding (synthetic element
                         facts copy the base position).
  * variable (attribute) -> the ``Attribute`` target (``self.x = ...``) or a
                         class-body assignment, qualified by the class path.

Facts that cannot be confidently mapped (a name with no binding in the AST: e.g.
``exec``-defined variables, or corpus name/type corruption) are returned as
``None`` and left untouched by callers.
"""

import ast
import re

_DEF_RE = re.compile(r"^(async\s+)?def\s+")
_SUBSCRIPT_TAIL = re.compile(r"\[[^\[\]]*\]$")


class Index:
    """AST index of every position a GT fact could point at, for one module."""

    def __init__(self, source: str):
        # Template sources carry ``<valueN>`` placeholders that are not valid
        # Python. They only ever sit on the RHS (value side), so substituting a
        # short literal preserves every binding-target line/column. No-op for
        # real generated files (which contain no placeholders).
        if "<value" in source:
            source = re.sub(r"<value\d+>", "1", source)
        self.lines = source.splitlines()
        tree = ast.parse(source)
        self.functions = {}      # def qualname (several spellings) -> name col positions
        self.params = {}         # (func qualname, arg)             -> arg positions
        self.lambda_params = {}  # arg                              -> lambda arg positions
        self.variables = {}      # plain target name                -> Name(Store) positions
        self.scoped_vars = {}    # (name, enclosing-func spelling)   -> Name(Store) positions
        self.attributes = {}     # Class...attr qualname            -> Attribute positions
        self.sub_roots = {}      # root of Subscript target         -> Subscript positions
        # class_stack = enclosing CLASS names; scope_stack = enclosing class AND
        # function names; scope_is_class = the immediate scope is a class body;
        # func_scope = qualname spellings of the nearest enclosing function ("" set
        # means module/class level) so a ``{variable, function}`` fact resolves to
        # the binding in THAT scope, not a same-named binding elsewhere.
        self._walk(tree, class_stack=(), scope_stack=(), scope_is_class=False,
                   func_scope=frozenset({""}))

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _add(table, key, lineno, col0):
        table.setdefault(key, []).append((lineno, col0))

    def _name_col0(self, node):
        """0-indexed column of a FunctionDef's *name* (after ``def``/``async def``)."""
        line = self.lines[node.lineno - 1]
        m = _DEF_RE.match(line[node.col_offset:])
        if m:
            return node.col_offset + m.end()
        return node.col_offset + 4  # fallback: plain ``def ``

    # -- walk -------------------------------------------------------------
    def _walk(self, node, class_stack, scope_stack, scope_is_class, func_scope):
        for child in ast.iter_child_nodes(node):
            self._visit(child, class_stack, scope_stack, scope_is_class, func_scope)

    def _qual_keys(self, name, class_stack, scope_stack):
        """All qualname spellings the corpus might use for a def: bare name,
        class-only path, and full (class+function) path."""
        return {
            name,
            ".".join(class_stack + (name,)),
            ".".join(scope_stack + (name,)),
        }

    def _visit(self, node, class_stack, scope_stack, scope_is_class, func_scope):
        if isinstance(node, ast.ClassDef):
            # class body is not a function scope (class-level names use "")
            self._walk(node, class_stack + (node.name,),
                       scope_stack + (node.name,), scope_is_class=True,
                       func_scope=frozenset({""}))
            return

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            keys = self._qual_keys(node.name, class_stack, scope_stack)
            col = self._name_col0(node)
            for key in keys:
                self._add(self.functions, key, node.lineno, col)
            for arg in self._all_args(node.args):
                for fn in keys:
                    self._add(self.params, (fn, arg.arg), arg.lineno, arg.col_offset)
            # the body's enclosing-function scope is this def's spellings
            self._walk(node, class_stack, scope_stack + (node.name,),
                       scope_is_class=False, func_scope=frozenset(keys))
            return

        if isinstance(node, ast.Lambda):
            for arg in self._all_args(node.args):
                self._add(self.lambda_params, arg.arg, arg.lineno, arg.col_offset)
            self._walk(node, class_stack, scope_stack, scope_is_class=False,
                       func_scope=func_scope)
            return

        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            self._add(self.variables, node.id, node.lineno, node.col_offset)
            for sc in func_scope:
                self._add(self.scoped_vars, (node.id, sc),
                          node.lineno, node.col_offset)
            # A class-body assignment (``class_var = ...``) is addressed as
            # ``MyClass.class_var`` in the GT — index it like an attribute.
            if scope_is_class and class_stack:
                qual = ".".join(class_stack + (node.id,))
                self._add(self.attributes, qual, node.lineno, node.col_offset)

        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            qual = self._attr_qualname(node, class_stack)
            if qual is not None:
                self._add(self.attributes, qual, node.lineno, node.col_offset)

        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            root = self._subscript_root(node)
            if root is not None:
                self._add(self.sub_roots, root, node.lineno, node.col_offset)

        # Descend through non-scope nodes PRESERVING the current scope flags so a
        # class-body ``class_var = ...`` is still seen as class-level.
        self._walk(node, class_stack, scope_stack, scope_is_class, func_scope)

    @staticmethod
    def _subscript_root(node):
        """Peel ``a['x'][0]`` down to its root expression text (``a``)."""
        while isinstance(node, ast.Subscript):
            node = node.value
        try:
            return ast.unparse(node)
        except Exception:
            return None

    @staticmethod
    def _all_args(arguments):
        out = list(getattr(arguments, "posonlyargs", []))
        out += list(arguments.args)
        if arguments.vararg:
            out.append(arguments.vararg)
        out += list(arguments.kwonlyargs)
        if arguments.kwarg:
            out.append(arguments.kwarg)
        return out

    @staticmethod
    def _attr_qualname(node, class_stack):
        """``self.a`` inside class B(inside A) -> ``A.B.a``; non-self falls back
        to ``<base>.<attr>`` via unparse."""
        attr = node.attr
        base = node.value
        if isinstance(base, ast.Name) and base.id in ("self", "cls"):
            if not class_stack:
                return None
            return ".".join(class_stack + (attr,))
        try:
            return ast.unparse(base) + "." + attr
        except Exception:
            return None


def _nearest(candidates, want_line, want_col, max_line_dist=4):
    """Pick the candidate (lineno, col0) closest to the existing GT position.

    A UNIQUE candidate is returned unconditionally: there is nothing to
    disambiguate, so the binding's position is the truth no matter how corrupted
    the old GT position was (e.g. a module ``a`` whose stale GT sat on line 1).
    With MULTIPLE candidates the old position is used to choose, and a match more
    than ``max_line_dist`` lines away is refused as too risky to trust.
    """
    if not candidates:
        return None
    uniq = set(candidates)
    if len(uniq) == 1:
        return next(iter(uniq))
    best = min(
        candidates,
        key=lambda c: (abs(c[0] - want_line), abs((c[1] + 1) - want_col), c[0], c[1]),
    )
    if abs(best[0] - want_line) > max_line_dist:
        return None
    return best


def _strip_subscripts(name):
    """``d['a']['b']`` -> ``d`` ; ``A.B.a['k']`` -> ``A.B.a`` ; ``e[0]`` -> ``e``."""
    prev = None
    while prev != name:
        prev = name
        name = _SUBSCRIPT_TAIL.sub("", name)
    return name


def _root_bindings(index, root):
    """Binding positions for a subscript root (a plain name or an attribute)."""
    if "." in root:
        return list(index.attributes.get(root, []))
    return list(index.variables.get(root, []))


def _to_gt(hit):
    if hit is None:
        return None
    lineno, col0 = hit
    return lineno, col0 + 1


def _candidates(fact, index):
    """Return ``(candidate_positions, group_key)`` for a GT fact. ``group_key``
    identifies facts competing for the SAME anchor set so the resolver can keep
    distinct facts (e.g. three identical lambdas on one line) apart."""
    if fact.get("function") == "lambda" and "variable" in fact:
        name = fact["variable"]
        return index.lambda_params.get(name, []), ("lambda", name)

    if "parameter" in fact:
        fn, pn = fact.get("function", ""), fact["parameter"]
        cands = index.params.get((fn, pn), []) + index.variables.get(pn, [])
        if "." in pn:  # mislabeled ``self`` attribute (``parameter:"Person.age"``)
            cands += index.attributes.get(pn, [])
        return cands, ("param", fn, pn)

    if "variable" in fact:
        name = fact["variable"]
        if "[" in name:
            root = _strip_subscripts(name)
            cands = list(index.sub_roots.get(root, [])) + _root_bindings(index, root)
            return cands, ("sub", root)
        if "." in name:
            return index.attributes.get(name, []), ("attr", name)
        # Plain name. A ``function`` qualifier scopes it to that function's body
        # (``{variable:"a", function:"MyClass.func2"}`` is the ``a`` inside func2,
        # not a same-named module-level ``a``). Fall back to the unscoped table
        # (plus lambda params) when the scope yields nothing.
        scope = fact.get("function", "")
        scoped = index.scoped_vars.get((name, scope))
        if scoped:
            return list(scoped), ("var", name, scope)
        return (index.variables.get(name, []) + index.lambda_params.get(name, []),
                ("var", name, scope))

    if "function" in fact:
        name = fact["function"]
        cands = index.functions.get(name, [])
        if not cands:  # a variable bound to a callable (recursive_tuple ``a = func``)
            cands = index.variables.get(name.split(".")[-1], [])
        return cands, ("func", name)

    return [], None


def resolve_facts(facts, index):
    """Map each fact to its AST-true (line, col), resolving same-anchor groups so
    distinct facts get distinct positions. Returns a list parallel to ``facts``
    of (line, col) or None (unmatched)."""
    groups = {}
    cand_by_key = {}
    for i, fact in enumerate(facts):
        cands, key = _candidates(fact, index)
        cand_by_key[key] = cands
        groups.setdefault(key, []).append(i)

    out: list = [None] * len(facts)
    for key, idxs in groups.items():
        cands = cand_by_key[key]
        olds = [(facts[i].get("line_number"), facts[i].get("col_offset")) for i in idxs]
        distinct = len(set(olds)) == len(olds)
        # Bijective case: N distinct facts and exactly N anchors -> pair by
        # position rank (both monotonic). The only way to separate identical
        # lambdas after a value-length shift moves them past each other's columns.
        if key is not None and len(idxs) == len(cands) and len(idxs) > 1 and distinct:
            order = sorted(range(len(idxs)), key=lambda k: olds[k])
            cands_sorted = sorted(cands)
            for rank, k in enumerate(order):
                out[idxs[k]] = _to_gt(cands_sorted[rank])
        else:
            for i in idxs:
                out[i] = _to_gt(
                    _nearest(cands, facts[i].get("line_number"),
                             facts[i].get("col_offset"))
                )
    return out


def rederive_kinds(source, facts):
    """Re-key GT records whose SHAPE (which kind-key they carry) is stale relative
    to the AST, in place. Returns ``facts``.

    A record carrying ONLY a ``function`` key (no ``variable``/``parameter``) maps
    to the benchmark adapter's ``kind="return"`` and MUST annotate a real function
    *definition*. If the generated AST instead shows the name is an assignment
    Store target (not a ``def``), the record is a mis-keyed variable: rename its
    ``function`` key to ``variable`` (preserving order, name, position and type).

    WHY: the ``recursive_tuple`` rebind ``a, (b, (c, d)) = func1, ...`` of a name
    that was previously *called* (``a()``) was historically emitted with the
    ``function`` key. The adapter then routes it through return-type resolution and
    reports the *called* function's return type (a concrete ``int``/``float``/...)
    instead of comparing the engine's correctly-emitted ``callable`` binding —
    a spurious TYPE_MISS. The fix is the same philosophy as ``rederive_facts``:
    stop trusting the inherited template shape; re-derive it from the AST.

    This is the generator-time hook: call it with the generated ``main.py`` text
    and the ground-truth list BEFORE ``rederive_facts`` (and before writing the
    ``*_gt.json``).
    """
    try:
        parse_src = re.sub(r"<value\d+>", "1", source) if "<value" in source else source
        tree = ast.parse(parse_src)
    except SyntaxError:
        return facts
    def_sites, store_targets = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            def_sites.add((node.lineno, node.name))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            store_targets.add((node.lineno, node.id))
    for fact in facts:
        if "function" in fact and "variable" not in fact and "parameter" not in fact:
            name, line = fact["function"], fact.get("line_number")
            if (line, name) not in def_sites and (line, name) in store_targets:
                rekeyed = [("variable" if k == "function" else k, v)
                           for k, v in list(fact.items())]
                fact.clear()
                fact.update(rekeyed)
    return facts


def rederive_facts(source, facts):
    """Rewrite ``line_number``/``col_offset`` of every fact in ``facts`` in place
    to the AST-true position derived from ``source``. Facts that cannot be mapped
    are left unchanged. Returns ``facts``.

    This is the generator-time hook: call it with the generated ``main.py`` text
    and the ground-truth list before writing the ``*_gt.json``.
    """
    index = Index(source)
    for fact, new in zip(facts, resolve_facts(facts, index)):
        if new is not None:
            fact["line_number"], fact["col_offset"] = new[0], new[1]
    return facts
