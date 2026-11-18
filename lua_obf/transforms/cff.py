"""Control-flow flattening.

Turns a flat list of statements into a `while STATE ~= 0 do if STATE==id
then ... end end` dispatcher, where STATE jumps between randomized ids in
execution order but the branches themselves are written out in a shuffled
textual order — so the linear structure of the original code no longer
matches the order it reads top-to-bottom in.

Safety rules (why these particular blocks, and not others):

1. We only ever flatten a block whose statements, scanned *without*
   descending into nested loop or function bodies (those are self-contained:
   a `break`/`continue` inside them can never target anything outside them),
   contain no `break` / `continue` / `goto` / `::label::`. Wrapping such a
   block in a brand-new `while` loop can't change what any break/goto in it
   binds to, because there isn't one. We never flatten a loop's own body
   directly — breaks aimed at *that* loop are far too common to risk.
2. `local x = v` inside one of the N branches would be scoped to that
   branch's own `if`-block in real Lua, invisible to the next branch — so
   every local declared directly in the block being flattened is hoisted to
   a single `local a, b, c` above the dispatcher, and the declaration itself
   becomes a plain assignment inside its branch. This is only safe because
   the renamer (which must run before this pass) already gives every local
   in the whole file a globally unique name, so hoisting can never shadow or
   collide with anything.
3. `local function f() ... end` is rewritten using the exact desugaring the
   Lua manual specifies for it (`local f; f = function() ... end`), so
   self-recursive calls still resolve correctly through the hoisted slot.
4. A block ending in `return` (the only place `return` can legally appear)
   keeps that `return` as the final statement of its branch with no trailing
   state-transition assignment after it, since Lua forbids any statement
   after `return` in the same block.
"""

import random

from ..astnodes import N

MIN_STMTS = 4


def _random_name():
    return "_" + "".join(random.choices("0123456789abcdef", k=8))


def _contains_escaping_control(stmts):
    """True if flattening `stmts` as one block could change what a
    break/continue/goto inside it binds to."""
    for s in stmts:
        k = s.kind
        if k in ("BreakStmt", "ContinueStmt", "GotoStmt", "LabelStmt"):
            return True
        if k == "IfStmt":
            for _, b in s.clauses:
                if _contains_escaping_control(b):
                    return True
            if s.else_body is not None and _contains_escaping_control(s.else_body):
                return True
        elif k == "DoStmt":
            if _contains_escaping_control(s.body):
                return True
        # WhileStmt/RepeatStmt/NumericForStmt/GenericForStmt/*function*: a
        # break/continue/goto inside those is already bound to them, not to
        # whatever block we're considering flattening around them, so we
        # deliberately don't recurse into those here.
    return False


class CFFPass:
    def __init__(self, probability=0.6, seed=None):
        self.probability = probability
        self.rng = random.Random(seed) if seed is not None else random

    def run(self, chunk):
        chunk.body = self.process_block(chunk.body, in_loop_body=False)
        return chunk

    # -- recurse into every statement's nested blocks first ----------------
    def process_block(self, stmts, in_loop_body):
        out = []
        for s in stmts:
            out.append(self.visit_stmt_children(s))
        if not in_loop_body:
            out = self.maybe_flatten(out)
        return out

    def visit_stmt_children(self, s):
        k = s.kind
        if k == "DoStmt":
            s.body = self.process_block(s.body, in_loop_body=False)
        elif k == "WhileStmt":
            s.body = self.process_block(s.body, in_loop_body=True)
        elif k == "RepeatStmt":
            s.body = self.process_block(s.body, in_loop_body=True)
        elif k == "NumericForStmt":
            s.body = self.process_block(s.body, in_loop_body=True)
        elif k == "GenericForStmt":
            s.body = self.process_block(s.body, in_loop_body=True)
        elif k == "IfStmt":
            s.clauses = [(c, self.process_block(b, in_loop_body=False)) for c, b in s.clauses]
            if s.else_body is not None:
                s.else_body = self.process_block(s.else_body, in_loop_body=False)
        elif k == "FunctionDeclStmt":
            s.body = self.process_block(s.body, in_loop_body=False)
        elif k == "LocalFunctionStmt":
            s.body = self.process_block(s.body, in_loop_body=False)
        elif k in ("LocalStmt", "AssignStmt"):
            for e in list(getattr(s, "exprs", [])):
                self.visit_expr_for_function_literals(e)
            if k == "AssignStmt":
                pass
        elif k == "ReturnStmt":
            for e in s.exprs:
                self.visit_expr_for_function_literals(e)
        elif k == "CallStmt":
            self.visit_expr_for_function_literals(s.call)
        return s

    def visit_expr_for_function_literals(self, e):
        # Function expressions can appear nested inside arbitrary
        # expressions (table fields, call args, ...); flatten their bodies
        # too, without needing a full expression walker duplicated here.
        k = e.kind
        if k == "FunctionExpr":
            e.body = self.process_block(e.body, in_loop_body=False)
        elif k == "Paren":
            self.visit_expr_for_function_literals(e.expr)
        elif k == "Index":
            self.visit_expr_for_function_literals(e.obj)
            if not e.dot:
                self.visit_expr_for_function_literals(e.key)
        elif k == "Call":
            self.visit_expr_for_function_literals(e.func)
            for a in e.args:
                self.visit_expr_for_function_literals(a)
        elif k == "MethodCall":
            self.visit_expr_for_function_literals(e.obj)
            for a in e.args:
                self.visit_expr_for_function_literals(a)
        elif k == "TableExpr":
            for f in e.fields:
                if f.ftype == "indexed":
                    self.visit_expr_for_function_literals(f.key)
                self.visit_expr_for_function_literals(f.value)
        elif k == "BinOp":
            self.visit_expr_for_function_literals(e.left)
            self.visit_expr_for_function_literals(e.right)
        elif k == "UnOp":
            self.visit_expr_for_function_literals(e.operand)

    # -- the actual flattening ---------------------------------------------
    def maybe_flatten(self, stmts):
        if len(stmts) < MIN_STMTS:
            return stmts
        if _contains_escaping_control(stmts):
            return stmts
        if self.rng.random() > self.probability:
            return stmts
        return self.flatten(stmts)

    def flatten(self, stmts):
        hoisted = []  # list of final (already-unique) local names
        branch_stmts = []  # list of statement-lists, one per state
        for s in stmts:
            k = s.kind
            if k == "LocalStmt":
                hoisted.extend(s.names)
                if s.exprs:
                    targets = [N("Name", name=nm) for nm in s.names]
                    branch_stmts.append([N("AssignStmt", targets=targets, exprs=s.exprs)])
                else:
                    branch_stmts.append([])
            elif k == "LocalFunctionStmt":
                hoisted.append(s.name)
                fn = N("FunctionExpr", params=s.params, vararg=s.vararg, body=s.body)
                branch_stmts.append([N("AssignStmt", targets=[N("Name", name=s.name)], exprs=[fn])])
            else:
                branch_stmts.append([s])
        # drop empty states (e.g. `local x` with no initializer) so we don't
        # waste a dispatcher id on a no-op branch
        branch_stmts = [bs for bs in branch_stmts if bs] or [[]]

        n = len(branch_stmts)
        ids = self.rng.sample(range(10000, 999999), n)
        state_name = _random_name()
        # Random terminal sentinel (outside the id range), not a fixed 0, so
        # the dispatcher carries no `~= 0` signature to fingerprint.
        term = self.rng.randint(1, 9999)

        if_clauses = []
        for i, bs in enumerate(branch_stmts):
            body = list(bs)
            ends_in_return = body and body[-1].kind == "ReturnStmt"
            if not ends_in_return:
                next_id = ids[i + 1] if i + 1 < n else term
                body.append(N("AssignStmt", targets=[N("Name", name=state_name)], exprs=[N("Number", raw=str(next_id))]))
            if_clauses.append((ids[i], body))

        display_order = list(range(n))
        self.rng.shuffle(display_order)
        shuffled_clauses = [
            (N("BinOp", op="==", left=N("Name", name=state_name), right=N("Number", raw=str(if_clauses[idx][0]))),
             if_clauses[idx][1])
            for idx in display_order
        ]

        # Two dispatcher shapes, chosen per flattened block, so the loop
        # structure itself varies instead of always being `while X ~= 0 do`.
        if self.rng.random() < 0.5:
            dispatcher = N(
                "WhileStmt",
                cond=N("BinOp", op="~=", left=N("Name", name=state_name),
                       right=N("Number", raw=str(term))),
                body=[N("IfStmt", clauses=shuffled_clauses, else_body=None)],
            )
        else:
            # `while true do ... if STATE == term then break end ... end`
            brk = (N("BinOp", op="==", left=N("Name", name=state_name),
                     right=N("Number", raw=str(term))), [N("BreakStmt")])
            clauses2 = list(shuffled_clauses)
            clauses2.insert(self.rng.randint(0, len(clauses2)), brk)
            dispatcher = N(
                "WhileStmt",
                cond=N("True"),
                body=[N("IfStmt", clauses=clauses2, else_body=None)],
            )

        result = []
        if hoisted:
            result.append(N("LocalStmt", names=list(hoisted), attribs=[None] * len(hoisted), exprs=[]))
        result.append(N("LocalStmt", names=[state_name], attribs=[None], exprs=[N("Number", raw=str(ids[0]))]))
        result.append(dispatcher)
        return result


def flatten_control_flow(chunk, probability=0.6, seed=None):
    return CFFPass(probability=probability, seed=seed).run(chunk)
