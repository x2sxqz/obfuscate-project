"""Numeric-literal obfuscation: replaces plain decimal integer literals with
an equivalent arithmetic expression tree. Hex literals, floats, and
scientific notation are left alone (floats especially: splitting them into
sub-expressions risks tiny IEEE-754 rounding differences from the original
value, which isn't worth the cosmetic win).

Every generated expression is wrapped in `Paren` so it's safe to drop into
any precedence context the original literal occupied (`10 * i` -> a bare
`5 + 5 * i` would silently change meaning; `(5 + 5) * i` can't)."""

import random

from ..astnodes import N


def _is_plain_decimal_int(raw):
    if raw.lower().startswith("0x"):
        return False
    if "." in raw or "e" in raw or "E" in raw:
        return False
    return raw.isdigit()


def _leaf(n):
    return N("Number", raw=str(n))


def _wrap(node):
    """Parenthesize unless it's already an atomic leaf, so nesting a BinOp
    inside another operator's operand can never shift precedence."""
    if node.kind in ("Number",):
        return node
    return N("Paren", expr=node)


def _build(n, depth, rng):
    if depth <= 0 or rng.random() < 0.35:
        return _leaf(n)
    choice = rng.choice(("add", "sub", "mul"))
    if choice == "add":
        a = rng.randint(-2000, 2000)
        b = n - a
        return N("BinOp", op="+", left=_wrap(_build(a, depth - 1, rng)), right=_wrap(_build(b, depth - 1, rng)))
    if choice == "sub":
        a = n + rng.randint(1, 2000)
        b = a - n
        return N("BinOp", op="-", left=_wrap(_build(a, depth - 1, rng)), right=_wrap(_build(b, depth - 1, rng)))
    # mul: only if n has a small usable factor, else fall back to add
    factors = [f for f in range(2, 13) if n != 0 and n % f == 0]
    if not factors:
        a = rng.randint(-2000, 2000)
        b = n - a
        return N("BinOp", op="+", left=_wrap(_build(a, depth - 1, rng)), right=_wrap(_build(b, depth - 1, rng)))
    f = rng.choice(factors)
    return N("BinOp", op="*", left=_wrap(_build(f, depth - 1, rng)), right=_wrap(_build(n // f, depth - 1, rng)))


class NumberObfuscator:
    def __init__(self, probability=0.85, max_depth=2, seed=None):
        self.probability = probability
        self.max_depth = max_depth
        self.rng = random.Random(seed) if seed is not None else random

    def run(self, chunk):
        chunk.body = self.visit_block(chunk.body)
        return chunk

    def visit_block(self, stmts):
        return [self.visit_stmt(s) for s in stmts]

    def visit_stmt(self, s):
        k = s.kind
        if k == "LocalStmt":
            s.exprs = [self.visit_expr(e) for e in s.exprs]
        elif k == "AssignStmt":
            s.targets = [self.visit_expr(t) for t in s.targets]
            s.exprs = [self.visit_expr(e) for e in s.exprs]
        elif k == "CallStmt":
            s.call = self.visit_expr(s.call)
        elif k == "DoStmt":
            s.body = self.visit_block(s.body)
        elif k == "WhileStmt":
            s.cond = self.visit_expr(s.cond)
            s.body = self.visit_block(s.body)
        elif k == "RepeatStmt":
            s.body = self.visit_block(s.body)
            s.cond = self.visit_expr(s.cond)
        elif k == "IfStmt":
            s.clauses = [(self.visit_expr(c), self.visit_block(b)) for c, b in s.clauses]
            if s.else_body is not None:
                s.else_body = self.visit_block(s.else_body)
        elif k == "NumericForStmt":
            s.start = self.visit_expr(s.start)
            s.stop = self.visit_expr(s.stop)
            if s.step is not None:
                s.step = self.visit_expr(s.step)
            s.body = self.visit_block(s.body)
        elif k == "GenericForStmt":
            s.exprs = [self.visit_expr(e) for e in s.exprs]
            s.body = self.visit_block(s.body)
        elif k == "FunctionDeclStmt":
            s.body = self.visit_block(s.body)
        elif k == "LocalFunctionStmt":
            s.body = self.visit_block(s.body)
        elif k == "ReturnStmt":
            s.exprs = [self.visit_expr(e) for e in s.exprs]
        elif k in ("BreakStmt", "ContinueStmt", "LabelStmt", "GotoStmt"):
            pass
        else:
            raise ValueError(f"numbers: unknown stmt kind {k}")
        return s

    def visit_expr(self, e):
        k = e.kind
        if k == "Number":
            if not _is_plain_decimal_int(e.raw):
                return e
            if self.rng.random() > self.probability:
                return e
            n = int(e.raw)
            tree = _build(n, self.max_depth, self.rng)
            if tree.kind == "Number":
                return tree
            return N("Paren", expr=tree)
        if k in ("Nil", "True", "False", "Vararg", "String", "Name"):
            return e
        if k == "Paren":
            e.expr = self.visit_expr(e.expr)
            return e
        if k == "Index":
            e.obj = self.visit_expr(e.obj)
            if not e.dot:
                e.key = self.visit_expr(e.key)
            return e
        if k == "Call":
            e.func = self.visit_expr(e.func)
            e.args = [self.visit_expr(a) for a in e.args]
            return e
        if k == "MethodCall":
            e.obj = self.visit_expr(e.obj)
            e.args = [self.visit_expr(a) for a in e.args]
            return e
        if k == "FunctionExpr":
            e.body = self.visit_block(e.body)
            return e
        if k == "TableExpr":
            for f in e.fields:
                if f.ftype == "indexed":
                    f.key = self.visit_expr(f.key)
                f.value = self.visit_expr(f.value)
            return e
        if k == "BinOp":
            e.left = self.visit_expr(e.left)
            e.right = self.visit_expr(e.right)
            return e
        if k == "UnOp":
            e.operand = self.visit_expr(e.operand)
            return e
        raise ValueError(f"numbers: unknown expr kind {k}")


def obfuscate_numbers(chunk, probability=0.85, max_depth=2, seed=None):
    return NumberObfuscator(probability=probability, max_depth=max_depth, seed=seed).run(chunk)
