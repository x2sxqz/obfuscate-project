"""Custom bytecode + VM mode (Luraph/Prometheus-style "vmify").

This is the strongest, and most dangerous-to-get-wrong, technique in the
pipeline, so its scope is deliberately bounded: a function is only
compiled to bytecode if it is **closed** and **single-value-safe**. Every
other function is left as ordinary Lua, untouched — this pass never
"partially" virtualizes something it can't fully prove safe.

A function qualifies only if ALL of the following hold:
  - no `...` (vararg)
  - no nested function definitions in its body (closures bring upvalue
    capture semantics a stack VM like this one doesn't model)
  - no `goto`/`::label::`
  - no generic `for .. in` (its 3-way iterator protocol's multi-value
    expansion isn't modelled)
  - no assignment to a true global (reading globals is fine and common —
    `print`, `string.format`, `math.floor`, ... — see below)
  - no place where Lua would expand a call/`...` into *multiple* values
    (a trailing call as the last arg of another call, the last field of a
    table constructor, or a `return`/`local`/assignment that depends on a
    call yielding more than one value) — this VM always truncates a call
    to its first return value, matching plain single-value use
  - every free identifier it reads either resolves to one of its own
    locals/params, or is a genuine global (not a local captured from an
    *enclosing* function — that would be an upvalue, which this VM doesn't
    support)

True globals the function reads (e.g. `print`, `table`, `math`) are
captured once as ordinary Lua upvalues via normal identifier syntax (e.g.
`local G = {print, table, math}`) rather than looked up by name at
runtime — this sidesteps the fact that `_G`/`getfenv` behave differently
across stock Lua, Lua 5.2+, and Luau, and is always correct because it's
just normal lexical name resolution happening once, outside the VM.
"""

import random

from ..astnodes import N
from ..parser import parse
from ..codegen import CodeGen, lua_string_literal


def _random_name():
    return "_" + "".join(random.choices("0123456789abcdef", k=8))


# ---------------------------------------------------------------------------
# Eligibility analysis
# ---------------------------------------------------------------------------

class Scope:
    __slots__ = ("parent", "names")

    def __init__(self, parent=None):
        self.parent = parent
        self.names = set()

    def declare(self, name):
        self.names.add(name)

    def resolve_depth(self):
        """Walk up, returning the chain of scopes (innermost first)."""
        chain = []
        s = self
        while s is not None:
            chain.append(s)
            s = s.parent
        return chain


class Disqualified(Exception):
    pass


def _has_multivalue_tail(e):
    return e.kind in ("Call", "MethodCall", "Vararg")


class EligibilityChecker:
    """Checks one function body for VM-virtualization eligibility, given the
    lexical scope active just *outside* the function.

    A name that isn't one of the function's own params/locals — whether a
    genuine global (`print`) or an upvalue captured from an enclosing Lua
    function — is still safe to *read*: it gets captured once as a plain
    Lua identifier reference (`local G = {print, someOuterLocal}`), which is
    just ordinary lexical closure capture and works identically either way.
    What's NOT safe is *writing* to one of those from inside the VM: our
    capture is a one-time snapshot, not a live reference, so an assignment
    wouldn't be visible outside the virtualized function. Writes to
    anything but the function's own locals are therefore disqualifying."""

    def __init__(self, enclosing_scope):
        self.enclosing = enclosing_scope

    def check(self, params, body):
        self.fn_scope = Scope(self.enclosing)
        for p in params:
            self.fn_scope.declare(p)
        try:
            self._block(body, self.fn_scope)
        except Disqualified:
            return False
        return True

    def _resolve(self, name, scope):
        """'local' if bound at/under this function's own top scope,
        'free' otherwise (a true global, or an upvalue from an enclosing
        Lua function — both captured the same safe way, see above)."""
        s = scope
        while s is not None:
            if name in s.names:
                return "local"
            if s is self.fn_scope:
                break
            s = s.parent
        return "free"

    def _block(self, stmts, scope):
        for s in stmts:
            self._stmt(s, scope)

    def _stmt(self, s, scope):
        k = s.kind
        if k == "LocalStmt":
            # Mismatched counts and multi-value tails are now handled by the
            # compiler's _compile_list (nil-pad / truncate / spread), so no
            # count restriction here.
            for e in s.exprs:
                self._expr(e, scope)
            for nm in s.names:
                scope.declare(nm)
            return
        if k == "AssignStmt":
            for t in s.targets:
                if t.kind == "Name":
                    if self._resolve(t.name, scope) != "local":
                        raise Disqualified()  # writing a free (global/upvalue) name: unsupported
                else:
                    self._expr(t, scope)
            for e in s.exprs:
                self._expr(e, scope)
            return
        if k == "CallStmt":
            self._expr(s.call, scope)
            return
        if k == "DoStmt":
            self._block(s.body, Scope(scope))
            return
        if k == "WhileStmt":
            self._expr(s.cond, scope)
            self._block(s.body, Scope(scope))
            return
        if k == "RepeatStmt":
            inner = Scope(scope)
            self._block(s.body, inner)
            self._expr(s.cond, inner)
            return
        if k == "IfStmt":
            for cond, body in s.clauses:
                self._expr(cond, scope)
                self._block(body, Scope(scope))
            if s.else_body is not None:
                self._block(s.else_body, Scope(scope))
            return
        if k == "NumericForStmt":
            self._expr(s.start, scope)
            self._expr(s.stop, scope)
            if s.step is not None:
                self._expr(s.step, scope)
            inner = Scope(scope)
            inner.declare(s.var)
            self._block(s.body, inner)
            return
        if k == "ReturnStmt":
            # Multiple return values and a multi-value tail are now supported.
            for e in s.exprs:
                self._expr(e, scope)
            return
        if k == "GenericForStmt":
            # for v1..vn in explist do body end. The VM's iterator setup
            # captures exactly the first 3 values of the header, and each
            # iteration call captures exactly n results. That's correct only
            # when the header doesn't rely on the LAST expression expanding
            # to fill remaining slots, and when there are no extra (>3)
            # expressions whose side effects we'd skip.
            exprs = s.exprs
            if len(exprs) > 3:
                raise Disqualified()
            if len(exprs) > 1 and exprs[-1].kind in ("Call", "MethodCall", "Vararg"):
                raise Disqualified()
            for e in exprs:
                self._expr(e, scope)
            inner = Scope(scope)
            for nm in s.names:
                inner.declare(nm)
            self._block(s.body, inner)
            return
        if k == "BreakStmt":
            return
        # ContinueStmt, GotoStmt, LabelStmt, FunctionDeclStmt,
        # LocalFunctionStmt: all still disqualifying (later stages).
        raise Disqualified()

    def _expr(self, e, scope):
        k = e.kind
        if k in ("Nil", "True", "False", "Number", "String"):
            return
        if k == "Vararg":
            return  # varargs are now supported
        if k == "Name":
            return  # both 'local' and 'free' reads are safe (see class docstring)
        if k == "Paren":
            self._expr(e.expr, scope)
            return
        if k == "Index":
            self._expr(e.obj, scope)
            if not e.dot:
                self._expr(e.key, scope)
            return
        if k == "Call":
            self._expr(e.func, scope)
            for a in e.args:
                self._expr(a, scope)  # multi-value tail now supported
            return
        if k == "MethodCall":
            self._expr(e.obj, scope)
            for a in e.args:
                self._expr(a, scope)
            return
        if k == "FunctionExpr":
            raise Disqualified()  # nested closures not modelled (Stage 3)
        if k == "TableExpr":
            for f in e.fields:
                if f.ftype == "indexed":
                    self._expr(f.key, scope)
                self._expr(f.value, scope)  # multi-value tail now supported
            return
        if k == "BinOp":
            self._expr(e.left, scope)
            self._expr(e.right, scope)
            return
        if k == "UnOp":
            self._expr(e.operand, scope)
            return
        raise Disqualified()


# ---------------------------------------------------------------------------
# Bytecode compiler
# ---------------------------------------------------------------------------

class FuncCompiler:
    def __init__(self, junk_prob=0.0, op_names=None):
        self.consts = []
        self.const_idx = {}
        self.globals = []
        self.global_idx = {}
        self.regs = {}
        self.code = []  # list of instruction lists, e.g. [op, a, b]
        self.loop_stack = []  # for break patching: list of lists-of-jump-indices
        self.junk_prob = junk_prob

    def k(self, value):
        # 1-indexed: these indices are used as K[idx] into a Lua table
        # literal built straight from self.consts (Lua tables are 1-based).
        key = (type(value).__name__, value)
        if key in self.const_idx:
            return self.const_idx[key]
        self.consts.append(value)
        idx = len(self.consts)
        self.const_idx[key] = idx
        return idx

    def g(self, name):
        if name in self.global_idx:
            return self.global_idx[name]
        self.globals.append(name)
        idx = len(self.globals)
        self.global_idx[name] = idx
        return idx

    def reg(self, name):
        # 1-indexed to match the VM runtime's `for i = 1, #ARGS do R[i] = ...`
        # parameter-loading loop (Lua tables are conventionally 1-based).
        if name not in self.regs:
            self.regs[name] = len(self.regs) + 1
        return self.regs[name]

    def emit(self, *ins):
        # Returns the Python (0-indexed) list position of the instruction
        # just appended — use this return value only as the `idx` argument
        # to patch(), never as an embedded jump-target value.
        self.code.append(list(ins))
        return len(self.code) - 1

    def here(self):
        # Returns the Lua (1-indexed) position the *next* emitted
        # instruction will occupy once the code list is serialized as a Lua
        # table — this is the right thing whenever the result is going to
        # be embedded as an actual `pc` jump-target VALUE in the bytecode
        # (as opposed to used as a Python list index).
        return len(self.code) + 1

    def patch(self, idx, target):
        self.code[idx][-1] = target

    # -- compile entry -------------------------------------------------
    def compile_function(self, params, body):
        for p in params:
            self.reg(p)
        self.compile_block(body)
        self.emit("RET0")
        # 4th value is nparams (fixed parameter count), used by the runtime to
        # split the packed args into registers + the varargs table.
        return self.code, self.consts, self.globals, len(params)

    @staticmethod
    def _is_multi(e):
        """True if `e` can yield more than one value in a tail position."""
        return e.kind in ("Call", "MethodCall", "Vararg")

    def compile_block(self, stmts):
        for s in stmts:
            # Junk NOPs between statements. Safe to insert anywhere in the
            # linear stream: a NOP just falls through to the pc
            # auto-increment, and every jump target is resolved from live
            # here()/emit() positions, so inserting one never desyncs an
            # address. A NOP must never be the block's trailing instruction
            # of a return-terminated path, but compile_block only inserts
            # *before* a statement, so the real statement always follows.
            if self.junk_prob and random.random() < self.junk_prob:
                self.emit("NOP")
            self.compile_stmt(s)

    def compile_stmt(self, s):
        k = s.kind
        if k == "LocalStmt":
            if s.exprs:
                self._compile_list(s.exprs, len(s.names))  # exactly N values on top
                for nm in reversed(s.names):
                    self.emit("POPREG", self.reg(nm))
            else:
                for nm in s.names:
                    self.reg(nm)
            return
        if k == "AssignStmt":
            self._compile_list(s.exprs, len(s.targets))  # exactly N values on top
            for t in reversed(s.targets):
                self.compile_store(t)
            return
        if k == "CallStmt":
            self.compile_expr(s.call)
            self.emit("POP")
            return
        if k == "DoStmt":
            self.compile_block(s.body)
            return
        if k == "WhileStmt":
            top = self.here()
            self.compile_expr(s.cond)
            jf = self.emit("JMPF", None)
            self.loop_stack.append([])
            self.compile_block(s.body)
            self.emit("JMP", top)
            end = self.here()
            self.patch(jf, end)
            for b in self.loop_stack.pop():
                self.patch(b, end)
            return
        if k == "RepeatStmt":
            top = self.here()
            self.loop_stack.append([])
            self.compile_block(s.body)
            self.compile_expr(s.cond)
            jf = self.emit("JMPF", top)
            end = self.here()
            for b in self.loop_stack.pop():
                self.patch(b, end)
            return
        if k == "IfStmt":
            end_jumps = []
            for cond, body in s.clauses:
                self.compile_expr(cond)
                jf = self.emit("JMPF", None)
                self.compile_block(body)
                end_jumps.append(self.emit("JMP", None))
                self.patch(jf, self.here())
            if s.else_body is not None:
                self.compile_block(s.else_body)
            end = self.here()
            for j in end_jumps:
                self.patch(j, end)
            return
        if k == "NumericForStmt":
            self.compile_expr(s.start)
            ctrl = self.reg(f"__for_{id(s)}")
            self.emit("POPREG", ctrl)
            self.compile_expr(s.stop)
            stop_r = self.reg(f"__forstop_{id(s)}")
            self.emit("POPREG", stop_r)
            if s.step is not None:
                self.compile_expr(s.step)
            else:
                self.emit("PUSHK", self.k(1))
            step_r = self.reg(f"__forstep_{id(s)}")
            self.emit("POPREG", step_r)
            top = self.here()  # Lua-1-indexed jump-target VALUE for the back-edge
            forcheck_idx = self.emit("FORCHECK", ctrl, stop_r, step_r, None)  # python idx, for patch()
            self.emit("PUSHREG", ctrl)
            self.emit("POPREG", self.reg(s.var))
            self.loop_stack.append([])
            self.compile_block(s.body)
            self.emit("FORSTEP", ctrl, step_r)
            self.emit("JMP", top)
            end = self.here()
            self.patch(forcheck_idx, end)
            for b in self.loop_stack.pop():
                self.patch(b, end)
            return
        if k == "GenericForStmt":
            # for v1..vn in explist do body end
            # Evaluate explist to exactly 3 values (iterator fn, state, ctrl).
            self._compile_explist3(s.exprs)
            reg_c = self.reg(f"__gfc_{id(s)}")
            reg_s = self.reg(f"__gfs_{id(s)}")
            reg_f = self.reg(f"__gff_{id(s)}")
            self.emit("POPREG", reg_c)  # ctrl (top)
            self.emit("POPREG", reg_s)  # state
            self.emit("POPREG", reg_f)  # iterator fn
            nvars = len(s.names)
            top = self.here()
            # call iterator: fn(state, ctrl) -> exactly nvars results
            self.emit("PUSHREG", reg_f)
            self.emit("PUSHREG", reg_s)
            self.emit("PUSHREG", reg_c)
            self.emit("CALLR", 2, nvars)  # 2 args, nvars results on stack (vN on top)
            # pop results into the loop-var regs (reverse order), keeping the
            # first result (control key) in a temp for the nil test.
            var_regs = [self.reg(nm) for nm in s.names]
            for r in reversed(var_regs[1:]):
                self.emit("POPREG", r)
            key_tmp = self.reg(f"__gfk_{id(s)}")
            self.emit("POPREG", key_tmp)  # first result
            # if key is nil -> end loop
            self.emit("PUSHREG", key_tmp)
            jnil = self.emit("JMPNIL", None)
            # not nil: var1 = key, ctrl = key
            self.emit("PUSHREG", key_tmp)
            self.emit("POPREG", var_regs[0])
            self.emit("PUSHREG", key_tmp)
            self.emit("POPREG", reg_c)
            self.loop_stack.append([])
            self.compile_block(s.body)
            self.emit("JMP", top)
            end = self.here()
            self.patch(jnil, end)
            for b in self.loop_stack.pop():
                self.patch(b, end)
            return
        if k == "ReturnStmt":
            exprs = s.exprs
            if not exprs:
                self.emit("RET0")
            elif len(exprs) == 1 and not self._is_multi(exprs[0]):
                self.compile_expr(exprs[0])
                self.emit("RET1")
            else:
                fixed = exprs[:-1]
                last = exprs[-1]
                for e in fixed:
                    self.compile_expr(e)
                if self._is_multi(last):
                    self.compile_expr_multi(last)  # pushes MC values, sets MC
                    self.emit("RETV", len(fixed))  # returns len(fixed) + MC
                else:
                    self.compile_expr(last)
                    self.emit("RETN", len(exprs))  # returns exactly len(exprs)
            return
        if k == "BreakStmt":
            j = self.emit("JMP", None)
            self.loop_stack[-1].append(j)
            return
        raise ValueError(f"vm: statement kind {k} should have been disqualified")

    def _compile_explist3(self, exprs):
        """Leave exactly 3 values on the stack (iterator fn, state, control)
        for a generic-for header. The common case is a single call
        (`ipairs(t)` / `pairs(t)` / a custom iterator factory) whose first
        three return values we want; otherwise each expression is a plain
        single value (e.g. `next, t`), padded with nil to three."""
        if len(exprs) == 1 and exprs[0].kind in ("Call", "MethodCall"):
            self._compile_call(exprs[0], nresults=3)
            return
        n = len(exprs)
        for e in exprs[:3]:
            self.compile_expr(e)
        # extra expressions beyond 3 would still be evaluated in Lua; the
        # eligibility check forbids that shape, so here n <= 3 always.
        for _ in range(n, 3):
            self.emit("PUSHK", self.k(None))

    def compile_store(self, t):
        if t.kind == "Name":
            self.emit("POPREG", self.reg(t.name))
            return
        if t.kind == "Index":
            # stack currently has [..., value]; need obj/key pushed under it,
            # so compile obj/key first into temp regs via a value-last swap:
            # simplest correct approach: stash value, compute obj/key, restore.
            tmp = self.reg(f"__store_tmp_{id(t)}")
            self.emit("POPREG", tmp)
            self.compile_expr(t.obj)
            if t.dot:
                self.emit("PUSHREG", tmp)
                self.emit("SETFIELDK", self.k(t.key.value))
            else:
                self.compile_expr(t.key)
                self.emit("PUSHREG", tmp)
                self.emit("SETINDEX")
            return
        raise ValueError("vm: bad assignment target")

    def _compile_call(self, e, nresults):
        """Compile a Call/MethodCall, capturing exactly `nresults` results on
        the stack (nil-padded / truncated). Used where a fixed number of
        return values is needed, e.g. the iterator setup of a generic-for.
        Generic-for headers forbid a multi-tail call, so no *V form here."""
        fixed, has_tail = self._emit_call_args(e)
        assert not has_tail
        self.emit("CALLR", fixed, nresults)

    def _emit_call_args(self, e):
        """Push the callee and its argument list. Returns (fixed, has_tail):
        `fixed` is the number of plain single-value arguments pushed, and if
        `has_tail` the last argument was multi-valued (its values are on the
        stack and MC is set), so total nargs = fixed + MC."""
        if e.kind == "Call":
            self.compile_expr(e.func)
            self_extra = 0
        else:  # MethodCall
            self.compile_expr(e.obj)
            self.emit("DUP")
            self.emit("GETFIELDK", self.k(e.method))
            self.emit("SWAP")  # stack: fn, self
            self_extra = 1
        args = e.args
        if args and self._is_multi(args[-1]):
            for a in args[:-1]:
                self.compile_expr(a)
            self.compile_expr_multi(args[-1])  # pushes MC values, sets MC
            return self_extra + len(args) - 1, True
        for a in args:
            self.compile_expr(a)
        return self_extra + len(args), False

    def _emit_call(self, e, multi):
        """Emit a call. `multi` selects whether all results stay on the stack
        (and MC is set) or the call is truncated to a single value."""
        fixed, has_tail = self._emit_call_args(e)
        if has_tail:
            self.emit("CALLVM" if multi else "CALLV", fixed)  # nargs = fixed + MC
        else:
            self.emit("CALLM" if multi else "CALL", fixed)

    def _compile_list(self, exprs, n):
        """Evaluate an expression list and leave EXACTLY `n` values on top of
        the stack (nil-padded or truncated), applying Lua's rule that only the
        last expression expands to multiple values."""
        if not exprs:
            for _ in range(n):
                self.emit("PUSHK", self.k(None))
            return
        fixed = exprs[:-1]
        last = exprs[-1]
        for e in fixed:
            self.compile_expr(e)
        if self._is_multi(last):
            self.compile_expr_multi(last)        # pushes MC values, sets MC
            self.emit("ADJV", len(fixed), n)     # make exactly n on top
        else:
            self.compile_expr(last)
            total = len(exprs)
            if total < n:
                for _ in range(n - total):
                    self.emit("PUSHK", self.k(None))
            else:
                for _ in range(total - n):
                    self.emit("POP")

    def compile_expr_multi(self, e):
        """Compile `e` so that ALL its values land on the stack and MC is set
        to the count. For non-multi expressions this still works via the
        single-value path with the caller treating it as one fixed value."""
        if e.kind == "Vararg":
            self.emit("VARARG")
        elif e.kind in ("Call", "MethodCall"):
            self._emit_call(e, multi=True)
        else:
            # not actually multi — caller handles it as a single fixed value
            self.compile_expr(e)

    def _compile_table(self, e):
        self.emit("NEWTABLE")
        pos = 1
        n = len(e.fields)
        for i, f in enumerate(e.fields):
            last = i == n - 1
            if f.ftype == "pos":
                if last and self._is_multi(f.value):
                    # table on stack; append all tail values from index `pos`
                    self.compile_expr_multi(f.value)
                    self.emit("SETLISTV", pos)
                else:
                    self.emit("DUP")
                    self.compile_expr(f.value)
                    self.emit("SETINDEXK", self.k(pos))
                    pos += 1
            elif f.ftype == "named":
                self.emit("DUP")
                self.compile_expr(f.value)
                self.emit("SETINDEXK", self.k(f.key))
            else:
                self.emit("DUP")
                self.compile_expr(f.key)
                self.compile_expr(f.value)
                self.emit("SETINDEX")

    def compile_expr(self, e):
        k = e.kind
        if k == "Nil":
            self.emit("PUSHK", self.k(None))
        elif k == "True":
            self.emit("PUSHK", self.k(True))
        elif k == "False":
            self.emit("PUSHK", self.k(False))
        elif k == "Number":
            raw = e.raw
            if raw.lower().startswith("0x"):
                v = int(raw, 16)
            elif "." in raw or "e" in raw or "E" in raw:
                v = float(raw)
            else:
                v = int(raw)
            self.emit("PUSHK", self.k(v))
        elif k == "String":
            self.emit("PUSHK", self.k(e.value))
        elif k == "Name":
            if e.name in self.regs or self._will_be_local(e.name):
                self.emit("PUSHREG", self.reg(e.name))
            else:
                self.emit("PUSHGLOBAL", self.g(e.name))
        elif k == "Paren":
            self.compile_expr(e.expr)
        elif k == "Index":
            self.compile_expr(e.obj)
            if e.dot:
                self.emit("GETFIELDK", self.k(e.key.value))
            else:
                self.compile_expr(e.key)
                self.emit("GETINDEX")
        elif k == "Vararg":
            self.emit("VARARG1")  # single-value context: first vararg only
        elif k == "Call" or k == "MethodCall":
            self._emit_call(e, multi=False)  # single result
        elif k == "TableExpr":
            self._compile_table(e)
        elif k == "BinOp":
            if e.op == "and":
                self.compile_expr(e.left)
                self.emit("DUP")
                jf = self.emit("JMPF", None)
                self.emit("POP")
                self.compile_expr(e.right)
                self.patch(jf, self.here())
            elif e.op == "or":
                self.compile_expr(e.left)
                self.emit("DUP")
                jt = self.emit("JMPT", None)
                self.emit("POP")
                self.compile_expr(e.right)
                self.patch(jt, self.here())
            else:
                self.compile_expr(e.left)
                self.compile_expr(e.right)
                self.emit("BINOP", self.k(e.op))
        elif k == "UnOp":
            self.compile_expr(e.operand)
            self.emit("UNOP", self.k(e.op))
        else:
            raise ValueError(f"vm: expr kind {k} should have been disqualified")

    def _will_be_local(self, name):
        return False  # all LocalStmt declarations register eagerly before use


# ---------------------------------------------------------------------------
# Emitting the bytecode + a Lua VM body to execute it
#
# Polymorphic VM ("opcode / instruction-set randomization"):
#   - Every opcode is a random integer, re-rolled on each build, so no two
#     builds share an instruction set and no single hand-written decompiler
#     can walk the bytecode of more than one build.
#   - Every number stored in the bytecode (opcodes AND operands — register
#     slots, constant-pool indices, jump targets, call arities) is encoded
#     as `value + KEY`, where KEY is a random per-build integer, and decoded
#     with `- KEY` at the one point each slot is read. So the bytecode array
#     is a wall of meaningless large numbers rather than readable opcodes.
#   - The dispatch branches are emitted in a shuffled order each build.
#   - A NOP opcode exists and junk NOPs are sprinkled into the stream, so the
#     instruction count and layout carry no reliable structure.
# None of this changes semantics — it only moves the encoding around — so the
# correctness guarantees of the compiler/eligibility analysis are untouched.
# ---------------------------------------------------------------------------

# Canonical opcode names the compiler emits. The runtime maps each to a
# random integer per build; order here is irrelevant.
_OPCODES = [
    "PUSHK", "PUSHREG", "POPREG", "PUSHGLOBAL", "POP", "DUP", "SWAP",
    "GETFIELDK", "GETINDEX", "SETFIELDK", "SETINDEX", "SETINDEXK",
    "NEWTABLE", "CALL", "BINOP", "UNOP", "JMP", "JMPF", "JMPT",
    "FORCHECK", "FORSTEP", "RET1", "RET0", "NOP",
    # generic-for support:
    "CALLR",   # call with a args, push exactly b results (nil-padded/truncated)
    "JMPNIL",  # pop; if the popped value is nil, jump (used for for-in end test)
    # varargs + multiple return values (Stage 2):
    "VARARG1",  # push the first vararg (single-value context)
    "VARARG",   # push all varargs, set MC = count
    "CALLV",    # call, nargs = a + MC, 1 result
    "CALLM",    # call, nargs = a, push ALL results, set MC
    "CALLVM",   # call, nargs = a + MC, push ALL results, set MC
    "RETN",     # return exactly a values from the top of stack
    "RETV",     # return a + MC values from the top of stack
    "ADJV",     # adjust a (fixed) + MC values on top to exactly b values
    "SETLISTV", # append MC values to the table below them, from index a
]

# Opcodes whose handler sets `pc` itself; the trailing auto-increment must
# skip them.
_PC_SETTING = {"JMP", "JMPF", "JMPT", "FORCHECK", "JMPNIL"}


def _handler_bodies(K, pk):
    """Returns {opcode_name: [lua_line, ...]} for the dispatch, with the
    per-build key `K` baked into every operand decode (`I[n]-K`) and `pk` the
    name of the value packer used by multi-result calls."""
    d = str(K)
    g = f"I[2]-{d}"
    return {
        "PUSHK":      ["SP=SP+1", f"ST[SP]=KC[{g}]"],
        "PUSHREG":    ["SP=SP+1", f"ST[SP]=R[{g}]"],
        "POPREG":     [f"R[{g}]=ST[SP]", "ST[SP]=nil", "SP=SP-1"],
        "PUSHGLOBAL": ["SP=SP+1", f"ST[SP]=GC[{g}]"],
        "POP":        ["ST[SP]=nil", "SP=SP-1"],
        "DUP":        ["SP=SP+1", "ST[SP]=ST[SP-1]"],
        "SWAP":       ["ST[SP],ST[SP-1]=ST[SP-1],ST[SP]"],
        "GETFIELDK":  [f"ST[SP]=ST[SP][KC[{g}]]"],
        "GETINDEX":   ["local _ky=ST[SP]", "ST[SP]=nil", "SP=SP-1", "ST[SP]=ST[SP][_ky]"],
        "SETFIELDK":  ["local _vv=ST[SP]", "ST[SP]=nil", "SP=SP-1",
                       "local _ob=ST[SP]", "ST[SP]=nil", "SP=SP-1", f"_ob[KC[{g}]]=_vv"],
        "SETINDEX":   ["local _vv=ST[SP]", "ST[SP]=nil", "SP=SP-1",
                       "local _ky=ST[SP]", "ST[SP]=nil", "SP=SP-1",
                       "local _ob=ST[SP]", "ST[SP]=nil", "SP=SP-1", "_ob[_ky]=_vv"],
        "SETINDEXK":  ["local _vv=ST[SP]", "ST[SP]=nil", "SP=SP-1",
                       "local _ob=ST[SP]", "ST[SP]=nil", "SP=SP-1", f"_ob[KC[{g}]]=_vv"],
        "NEWTABLE":   ["SP=SP+1", "ST[SP]={}"],
        "CALL":       [f"local _n=({g})", "local _ar={}",
                       "for _ix=_n,1,-1 do _ar[_ix]=ST[SP] ST[SP]=nil SP=SP-1 end",
                       "local _fn=ST[SP]", "ST[SP]=nil", "SP=SP-1",
                       "local _rr=_fn(table.unpack(_ar,1,_n))", "SP=SP+1", "ST[SP]=_rr"],
        "BINOP":      ["local _b=ST[SP]", "ST[SP]=nil", "SP=SP-1", "local _a=ST[SP]",
                       f"local _o=KC[{g}]",
                       'if _o=="+" then ST[SP]=_a+_b '
                       'elseif _o=="-" then ST[SP]=_a-_b '
                       'elseif _o=="*" then ST[SP]=_a*_b '
                       'elseif _o=="/" then ST[SP]=_a/_b '
                       'elseif _o=="%" then ST[SP]=_a%_b '
                       'elseif _o=="^" then ST[SP]=_a^_b '
                       'elseif _o==".." then ST[SP]=_a.._b '
                       'elseif _o=="==" then ST[SP]=_a==_b '
                       'elseif _o=="~=" then ST[SP]=_a~=_b '
                       'elseif _o=="<" then ST[SP]=_a<_b '
                       'elseif _o==">" then ST[SP]=_a>_b '
                       'elseif _o=="<=" then ST[SP]=_a<=_b '
                       'elseif _o==">=" then ST[SP]=_a>=_b end'],
        "UNOP":       [f"local _o=KC[{g}]",
                       'if _o=="-" then ST[SP]=-ST[SP] '
                       'elseif _o=="not" then ST[SP]=not ST[SP] '
                       'elseif _o=="#" then ST[SP]=#ST[SP] end'],
        "JMP":        [f"pc={g}"],
        "JMPF":       ["local _v=ST[SP]", "ST[SP]=nil", "SP=SP-1",
                       f"if _v==nil or _v==false then pc={g} else pc=pc+1 end"],
        "JMPT":       ["local _v=ST[SP]", "ST[SP]=nil", "SP=SP-1",
                       f"if _v~=nil and _v~=false then pc={g} else pc=pc+1 end"],
        "FORCHECK":   [f"local _c1,_c2,_c3=R[{g}],R[I[3]-{d}],R[I[4]-{d}]",
                       "if (_c3>0 and _c1<=_c2) or (_c3<0 and _c1>=_c2) or _c3==0 then pc=pc+1 "
                       f"else pc=I[5]-{d} end"],
        "FORSTEP":    [f"R[{g}]=R[{g}]+R[I[3]-{d}]"],
        "RET1":       ["return ST[SP]"],
        "RET0":       ["return nil"],
        "NOP":        ["ST[SP]=ST[SP]"],  # harmless; falls through to pc auto-increment
        "CALLR":      [f"local _na=({g})", f"local _nr=(I[3]-{d})", "local _ar={}",
                       "for _ix=_na,1,-1 do _ar[_ix]=ST[SP] ST[SP]=nil SP=SP-1 end",
                       "local _fn=ST[SP]", "ST[SP]=nil", "SP=SP-1",
                       "local _rs={_fn(table.unpack(_ar,1,_na))}",
                       "for _ix=1,_nr do SP=SP+1 ST[SP]=_rs[_ix] end"],
        "JMPNIL":     ["local _v=ST[SP]", "ST[SP]=nil", "SP=SP-1",
                       f"if _v==nil then pc={g} else pc=pc+1 end"],
        "VARARG1":    ["SP=SP+1", "ST[SP]=VA[1]"],
        "VARARG":     ["for _ix=1,VA.n do SP=SP+1 ST[SP]=VA[_ix] end", "MC=VA.n"],
        "CALLV":      [f"local _na=({g})+MC", "local _ar={}",
                       "for _ix=_na,1,-1 do _ar[_ix]=ST[SP] ST[SP]=nil SP=SP-1 end",
                       "local _fn=ST[SP]", "ST[SP]=nil", "SP=SP-1",
                       "local _r=_fn(table.unpack(_ar,1,_na))", "SP=SP+1", "ST[SP]=_r"],
        "CALLM":      [f"local _na=({g})", "local _ar={}",
                       "for _ix=_na,1,-1 do _ar[_ix]=ST[SP] ST[SP]=nil SP=SP-1 end",
                       "local _fn=ST[SP]", "ST[SP]=nil", "SP=SP-1",
                       f"local _rs={pk}(_fn(table.unpack(_ar,1,_na)))",
                       "for _ix=1,_rs.n do SP=SP+1 ST[SP]=_rs[_ix] end", "MC=_rs.n"],
        "CALLVM":     [f"local _na=({g})+MC", "local _ar={}",
                       "for _ix=_na,1,-1 do _ar[_ix]=ST[SP] ST[SP]=nil SP=SP-1 end",
                       "local _fn=ST[SP]", "ST[SP]=nil", "SP=SP-1",
                       f"local _rs={pk}(_fn(table.unpack(_ar,1,_na)))",
                       "for _ix=1,_rs.n do SP=SP+1 ST[SP]=_rs[_ix] end", "MC=_rs.n"],
        "RETN":       [f"return table.unpack(ST,SP-({g})+1,SP)"],
        "RETV":       [f"return table.unpack(ST,SP-(({g})+MC)+1,SP)"],
        "ADJV":       [f"local _bse=SP-({g})-MC", f"local _tgt=_bse+(I[3]-{d})",
                       "if _tgt>=SP then for _ix=SP+1,_tgt do ST[_ix]=nil end "
                       "else for _ix=_tgt+1,SP do ST[_ix]=nil end end", "SP=_tgt"],
        "SETLISTV":   ["local _tb=ST[SP-MC]",
                       f"for _ix=1,MC do _tb[({g})+_ix-1]=ST[SP-MC+_ix] end",
                       "for _ix=1,MC do ST[SP]=nil SP=SP-1 end"],
    }


def _build_runtime(vmrun, pk, op_map, key, order):
    """Generate the Lua source of the VM interpreter for this build.

    vmrun:  name of the interpreter function
    pk:     name of the arg/return packer (captures count incl. trailing nils)
    op_map: {opcode_name: random_int}   (the decoded opcode value)
    key:    per-build integer added to every stored bytecode number
    order:  opcode names in the (shuffled) order their branches appear

    The 4th VMRUN parameter NP is the function's fixed parameter count: R gets
    R[1..NP] from the packed args AG, and anything past NP forms the varargs
    table VA (with VA.n), so `...` inside the bytecode reads true counts.
    """
    bodies = _handler_bodies(key, pk)
    branches = []
    for i, name in enumerate(order):
        kw = "if" if i == 0 else "elseif"
        lines = "\n      ".join(bodies[name])
        branches.append(f"    {kw} op == {op_map[name]} then\n      {lines}")
    dispatch = "\n".join(branches) + "\n    end"
    skip = " and ".join(f"op ~= {op_map[n]}" for n in sorted(_PC_SETTING, key=lambda x: op_map[x]))
    return (
        f"local {pk} = function(...) return {{n = select('#', ...), ...}} end\n"
        f"local function {vmrun}(CODE, KC, GC, NP, AG)\n"
        f"  local R = {{}}\n"
        f"  local NA = AG.n\n"
        f"  for _p = 1, NP do R[_p] = AG[_p] end\n"
        f"  local VA = {{}}\n"
        f"  local _vn = 0\n"
        f"  for _p = NP + 1, NA do _vn = _vn + 1 VA[_vn] = AG[_p] end\n"
        f"  VA.n = _vn\n"
        f"  local ST = {{}}\n"
        f"  local SP = 0\n"
        f"  local MC = 0\n"
        f"  local pc = 1\n"
        f"  while true do\n"
        f"    local I = CODE[pc]\n"
        f"    local op = I[1] - {key}\n"
        f"{dispatch}\n"
        f"    if {skip} then pc = pc + 1 end\n"
        f"  end\n"
        f"end\n"
    )


def _lua_literal(v):
    if v is None:
        return "nil"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, str):
        return lua_string_literal(v)
    if isinstance(v, float):
        return repr(v)
    return str(v)


class VmPass:
    def __init__(self, junk_prob=0.25):
        self.vmrun_name = _random_name()
        self.pk_name = _random_name()
        self.used = False
        self.junk_prob = junk_prob
        # Per-build instruction set: each opcode gets a distinct random
        # integer, and the branch order is shuffled. Both differ every build.
        nums = random.sample(range(1, 4096), len(_OPCODES))
        self.op_map = dict(zip(_OPCODES, nums))
        self.order = list(_OPCODES)
        random.shuffle(self.order)
        # Per-build additive key applied to every number in the bytecode.
        self.key = random.randint(1, 1_000_000)

    def run(self, chunk):
        chunk.body = self.visit_block(chunk.body, Scope(None))
        if self.used:
            runtime_src = _build_runtime(self.vmrun_name, self.pk_name, self.op_map, self.key, self.order)
            header = parse(runtime_src).body
            chunk.body = header + chunk.body
        return chunk

    # Walk the tree with a real scope model so EligibilityChecker can tell
    # locals-of-this-function from upvalues-from-outside.
    def visit_block(self, stmts, scope):
        return [self.visit_stmt(s, scope) for s in stmts]

    def visit_stmt(self, s, scope):
        k = s.kind
        if k == "LocalStmt":
            for nm in s.names:
                scope.declare(nm)
        elif k == "AssignStmt":
            pass
        elif k == "DoStmt":
            s.body = self.visit_block(s.body, Scope(scope))
        elif k == "WhileStmt":
            s.body = self.visit_block(s.body, Scope(scope))
        elif k == "RepeatStmt":
            s.body = self.visit_block(s.body, Scope(scope))
        elif k == "NumericForStmt":
            inner = Scope(scope)
            inner.declare(s.var)
            s.body = self.visit_block(s.body, inner)
        elif k == "GenericForStmt":
            inner = Scope(scope)
            for nm in s.names:
                inner.declare(nm)
            s.body = self.visit_block(s.body, inner)
        elif k == "IfStmt":
            s.clauses = [(c, self.visit_block(b, Scope(scope))) for c, b in s.clauses]
            if s.else_body is not None:
                s.else_body = self.visit_block(s.else_body, Scope(scope))
        elif k == "FunctionDeclStmt":
            new_body = self.try_vmify(s, scope)
            s.body = new_body if new_body is not None else self.visit_block(s.body, self._fn_scope(s.params, scope))
        elif k == "LocalFunctionStmt":
            scope.declare(s.name)
            new_body = self.try_vmify(s, scope)
            s.body = new_body if new_body is not None else self.visit_block(s.body, self._fn_scope(s.params, scope))
        # ReturnStmt/BreakStmt/ContinueStmt/Goto/Label: nothing to recurse into structurally
        return s

    def _fn_scope(self, params, scope):
        inner = Scope(scope)
        for p in params:
            inner.declare(p)
        return inner

    def try_vmify(self, node, enclosing_scope):
        """If `node` (a FunctionDeclStmt or LocalFunctionStmt) is eligible,
        compile its body to bytecode, rewrite the wrapper to a vararg packer
        that invokes the VM, and return the new body. Otherwise return None
        and leave the node untouched."""
        params, body = node.params, node.body
        if not body:
            return None
        if not EligibilityChecker(enclosing_scope).check(params, body):
            return None
        fc = FuncCompiler(junk_prob=self.junk_prob, op_names=_OPCODES)
        try:
            code, consts, globs, nparams = fc.compile_function(params, body)
        except Exception:
            return None
        self.used = True
        # Each slot = value + KEY. The opcode (slot 0) maps through op_map
        # first; operands (slots 1+) are raw ints (indices / jump targets /
        # arities), all shifted by the same key and decoded with `- KEY` at
        # read time in the runtime.
        k = self.key
        instrs = []
        for ins in code:
            parts = [str(self.op_map[ins[0]] + k)]
            parts.extend(self._operand(x) for x in ins[1:])
            instrs.append("{" + ",".join(parts) + "}")
        code_lit = "{" + ",".join(instrs) + "}"
        const_lit = "{" + ",".join(_lua_literal(c) for c in consts) + "}"
        global_lit = "{" + ",".join(globs) + "}"
        # Rewrite the wrapper to `(...)` and pass ALL incoming args packed (so
        # the true count, trailing nils included, survives). For `:` methods
        # `self` is bound implicitly and captured lexically in globals, so it
        # is not part of `...` and nparams excludes it.
        node.params = []
        node.vararg = True
        args_lit = f"{self.pk_name}(...)"
        src = (f"return {self.vmrun_name}({code_lit}, {const_lit}, {global_lit}, "
               f"{nparams}, {args_lit})")
        return parse(src).body

    def _operand(self, x):
        # Patching must have replaced every None jump target by now; guard
        # anyway so a bug surfaces as a parse failure, not silent corruption.
        if x is None:
            x = 0
        return str(x + self.key)


def vmify(chunk, junk_prob=0.25):
    return VmPass(junk_prob=junk_prob).run(chunk)
