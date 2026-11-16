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
            # Our stack VM maps each expr 1:1 to its target register with
            # single-value truncation (exactly matching real Lua when the
            # counts match); it has no padding/truncation logic for a count
            # mismatch, so any mismatch at all is disqualifying — not just
            # the multi-value-spread case.
            if s.exprs and len(s.exprs) != len(s.names):
                raise Disqualified()
            for e in s.exprs:
                self._expr(e, scope)
            for nm in s.names:
                scope.declare(nm)
            return
        if k == "AssignStmt":
            if len(s.exprs) != len(s.targets):
                raise Disqualified()
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
            if len(s.exprs) > 1:
                raise Disqualified()
            if len(s.exprs) == 1:
                if _has_multivalue_tail(s.exprs[0]):
                    raise Disqualified()
                self._expr(s.exprs[0], scope)
            return
        if k == "BreakStmt":
            return
        # GenericForStmt, ContinueStmt, GotoStmt, LabelStmt, FunctionDeclStmt,
        # LocalFunctionStmt: all disqualifying.
        raise Disqualified()

    def _expr(self, e, scope):
        k = e.kind
        if k in ("Nil", "True", "False", "Number", "String"):
            return
        if k == "Vararg":
            raise Disqualified()
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
            for i, a in enumerate(e.args):
                if i == len(e.args) - 1 and _has_multivalue_tail(a):
                    raise Disqualified()
                self._expr(a, scope)
            return
        if k == "MethodCall":
            self._expr(e.obj, scope)
            for i, a in enumerate(e.args):
                if i == len(e.args) - 1 and _has_multivalue_tail(a):
                    raise Disqualified()
                self._expr(a, scope)
            return
        if k == "FunctionExpr":
            raise Disqualified()  # nested closures not modelled
        if k == "TableExpr":
            for i, f in enumerate(e.fields):
                if f.ftype == "indexed":
                    self._expr(f.key, scope)
                if f.ftype == "pos" and i == len(e.fields) - 1 and _has_multivalue_tail(f.value):
                    raise Disqualified()
                self._expr(f.value, scope)
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
    def __init__(self):
        self.consts = []
        self.const_idx = {}
        self.globals = []
        self.global_idx = {}
        self.regs = {}
        self.code = []  # list of instruction lists, e.g. [op, a, b]
        self.loop_stack = []  # for break patching: list of lists-of-jump-indices

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
        return self.code, self.consts, self.globals, len(self.regs), list(params)

    def compile_block(self, stmts):
        for s in stmts:
            self.compile_stmt(s)

    def compile_stmt(self, s):
        k = s.kind
        if k == "LocalStmt":
            if s.exprs:
                for e in s.exprs:
                    self.compile_expr(e)
                # stack is LIFO (last pushed expr is on top), so pop names
                # in reverse to match each name with its own expr by position
                for nm in reversed(s.names):
                    self.emit("POPREG", self.reg(nm))
            else:
                for nm in s.names:
                    self.reg(nm)
            return
        if k == "AssignStmt":
            for e in s.exprs:
                self.compile_expr(e)
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
        if k == "ReturnStmt":
            if s.exprs:
                self.compile_expr(s.exprs[0])
                self.emit("RET1")
            else:
                self.emit("RET0")
            return
        if k == "BreakStmt":
            j = self.emit("JMP", None)
            self.loop_stack[-1].append(j)
            return
        raise ValueError(f"vm: statement kind {k} should have been disqualified")

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
        elif k == "Call":
            self.compile_expr(e.func)
            for a in e.args:
                self.compile_expr(a)
            self.emit("CALL", len(e.args))
        elif k == "MethodCall":
            self.compile_expr(e.obj)
            self.emit("DUP")
            self.emit("GETFIELDK", self.k(e.method))
            self.emit("SWAP")
            for a in e.args:
                self.compile_expr(a)
            self.emit("CALL", len(e.args) + 1)
        elif k == "TableExpr":
            self.emit("NEWTABLE")
            pos = 1
            for f in e.fields:
                if f.ftype == "pos":
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
# ---------------------------------------------------------------------------

_VM_RUNTIME_TEMPLATE = """\
local function {vmrun}(CODE, K, G, NREG, ARGS)
  local R = {{}}
  for i = 1, #ARGS do R[i] = ARGS[i] end
  local ST = {{}}
  local SP = 0
  local pc = 1
  while true do
    local ins = CODE[pc]
    local op = ins[1]
    if op == "PUSHK" then
      SP = SP + 1; ST[SP] = K[ins[2]]
    elseif op == "PUSHREG" then
      SP = SP + 1; ST[SP] = R[ins[2]]
    elseif op == "POPREG" then
      R[ins[2]] = ST[SP]; ST[SP] = nil; SP = SP - 1
    elseif op == "PUSHGLOBAL" then
      SP = SP + 1; ST[SP] = G[ins[2]]
    elseif op == "POP" then
      ST[SP] = nil; SP = SP - 1
    elseif op == "DUP" then
      SP = SP + 1; ST[SP] = ST[SP - 1]
    elseif op == "SWAP" then
      ST[SP], ST[SP - 1] = ST[SP - 1], ST[SP]
    elseif op == "GETFIELDK" then
      ST[SP] = ST[SP][K[ins[2]]]
    elseif op == "GETINDEX" then
      local key = ST[SP]; ST[SP] = nil; SP = SP - 1
      ST[SP] = ST[SP][key]
    elseif op == "SETFIELDK" then
      local val = ST[SP]; ST[SP] = nil; SP = SP - 1
      local obj = ST[SP]; ST[SP] = nil; SP = SP - 1
      obj[K[ins[2]]] = val
    elseif op == "SETINDEX" then
      local val = ST[SP]; ST[SP] = nil; SP = SP - 1
      local key = ST[SP]; ST[SP] = nil; SP = SP - 1
      local obj = ST[SP]; ST[SP] = nil; SP = SP - 1
      obj[key] = val
    elseif op == "SETINDEXK" then
      local val = ST[SP]; ST[SP] = nil; SP = SP - 1
      local obj = ST[SP]; ST[SP] = nil; SP = SP - 1
      obj[K[ins[2]]] = val
    elseif op == "NEWTABLE" then
      SP = SP + 1; ST[SP] = {{}}
    elseif op == "CALL" then
      local n = ins[2]
      local args = {{}}
      for i = n, 1, -1 do args[i] = ST[SP]; ST[SP] = nil; SP = SP - 1 end
      local fn = ST[SP]; ST[SP] = nil; SP = SP - 1
      local result = fn(table.unpack(args, 1, n))
      SP = SP + 1; ST[SP] = result
    elseif op == "BINOP" then
      local b = ST[SP]; ST[SP] = nil; SP = SP - 1
      local a = ST[SP]
      local o = K[ins[2]]
      if o == "+" then ST[SP] = a + b
      elseif o == "-" then ST[SP] = a - b
      elseif o == "*" then ST[SP] = a * b
      elseif o == "/" then ST[SP] = a / b
      elseif o == "%" then ST[SP] = a % b
      elseif o == "^" then ST[SP] = a ^ b
      elseif o == ".." then ST[SP] = a .. b
      elseif o == "==" then ST[SP] = a == b
      elseif o == "~=" then ST[SP] = a ~= b
      elseif o == "<" then ST[SP] = a < b
      elseif o == ">" then ST[SP] = a > b
      elseif o == "<=" then ST[SP] = a <= b
      elseif o == ">=" then ST[SP] = a >= b
      end
    elseif op == "UNOP" then
      local o = K[ins[2]]
      if o == "-" then ST[SP] = -ST[SP]
      elseif o == "not" then ST[SP] = not ST[SP]
      elseif o == "#" then ST[SP] = #ST[SP]
      end
    elseif op == "JMP" then
      pc = ins[2]
    elseif op == "JMPF" then
      local v = ST[SP]; ST[SP] = nil; SP = SP - 1
      if v == nil or v == false then pc = ins[2] else pc = pc + 1 end
    elseif op == "JMPT" then
      local v = ST[SP]; ST[SP] = nil; SP = SP - 1
      if v ~= nil and v ~= false then pc = ins[2] else pc = pc + 1 end
    elseif op == "FORCHECK" then
      local ctrl, stop, step = R[ins[2]], R[ins[3]], R[ins[4]]
      if (step > 0 and ctrl <= stop) or (step < 0 and ctrl >= stop) or step == 0 then
        pc = pc + 1
      else
        pc = ins[5]
      end
    elseif op == "FORSTEP" then
      R[ins[2]] = R[ins[2]] + R[ins[3]]
    elseif op == "RET1" then
      return ST[SP]
    elseif op == "RET0" then
      return nil
    end
    if op ~= "JMP" and op ~= "JMPF" and op ~= "JMPT" and op ~= "FORCHECK" then
      pc = pc + 1
    end
  end
end
"""


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
    def __init__(self):
        self.vmrun_name = _random_name()
        self.used = False

    def run(self, chunk):
        chunk.body = self.visit_block(chunk.body, Scope(None))
        if self.used:
            header = parse(_VM_RUNTIME_TEMPLATE.format(vmrun=self.vmrun_name)).body
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
            s.body = self.try_vmify(s.params, s.body, scope) or self.visit_block(s.body, self._fn_scope(s.params, scope))
        elif k == "LocalFunctionStmt":
            scope.declare(s.name)
            s.body = self.try_vmify(s.params, s.body, scope) or self.visit_block(s.body, self._fn_scope(s.params, scope))
        # ReturnStmt/BreakStmt/ContinueStmt/Goto/Label: nothing to recurse into structurally
        return s

    def _fn_scope(self, params, scope):
        inner = Scope(scope)
        for p in params:
            inner.declare(p)
        return inner

    def try_vmify(self, params, body, enclosing_scope):
        """Returns a replacement body (a single CallStmt-free statement list
        that invokes the VM) if eligible, else None (caller keeps recursing
        normally)."""
        if not body:
            return None
        if not EligibilityChecker(enclosing_scope).check(params, body):
            return None
        fc = FuncCompiler()
        try:
            code, consts, globs, nreg, _params = fc.compile_function(params, body)
        except Exception:
            return None
        self.used = True
        code_lit = "{" + ",".join(
            "{" + ",".join([f'"{ins[0]}"'] + [self._operand(x) for x in ins[1:]]) + "}"
            for ins in code
        ) + "}"
        const_lit = "{" + ",".join(_lua_literal(c) for c in consts) + "}"
        global_lit = "{" + ",".join(globs) + "}"
        args_lit = "{" + ",".join(params) + "}"
        src = f"return {self.vmrun_name}({code_lit}, {const_lit}, {global_lit}, {nreg}, {args_lit})"
        new_body = parse(src).body
        return new_body

    def _operand(self, x):
        if x is None:
            return "0"  # patched jump target of 0 is unreachable (pc starts at 1) if ever left
        return str(x)


def vmify(chunk):
    return VmPass().run(chunk)
