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

import base64
import random

from ..astnodes import N
from ..parser import parse
from ..codegen import CodeGen, lua_string_literal


_NAME_FIRST = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
_NAME_REST = _NAME_FIRST + "0123456789"


def _random_name():
    # Varied mixed-case/digit/underscore identifiers (not uniform hex), so the
    # VM's own helper/table names blend with the mixed-style renamed code.
    n = random.randint(12, 22)
    return random.choice(_NAME_FIRST) + "".join(random.choices(_NAME_REST, k=n - 1))


def _decl_name(node):
    """Best-effort name of a function declaration: the local-function name, or
    the dotted/`:` target of `function a.b.c()` / `function a:m()`."""
    if node.kind == "LocalFunctionStmt":
        return node.name
    t = getattr(node, "target", None)
    parts = []
    while t is not None and t.kind == "Index":
        parts.append(t.key.value)
        t = t.obj
    if t is not None and t.kind == "Name":
        parts.append(t.name)
    return ".".join(reversed(parts)) if parts else None


def mark_skip_functions(chunk, names):
    """Flag every function declaration whose name (or last dotted segment) is
    in `names` with `skip_vm=True`, so vmify leaves it native. Must run BEFORE
    renaming, while the original names are still present."""
    names = set(names or ())
    if not names:
        return chunk

    def walk(stmts):
        for s in stmts:
            k = s.kind
            if k in ("FunctionDeclStmt", "LocalFunctionStmt"):
                nm = _decl_name(s)
                if nm and (nm in names or nm.split(".")[-1] in names):
                    s.skip_vm = True
                walk(s.body)
            elif k in ("DoStmt", "WhileStmt", "RepeatStmt", "NumericForStmt", "GenericForStmt"):
                walk(s.body)
            elif k == "IfStmt":
                for _, b in s.clauses:
                    walk(b)
                if s.else_body is not None:
                    walk(s.else_body)
    walk(chunk.body)
    return chunk


def _env_gate():
    """A Lua expression that is 0 on real Roblox and a nonzero corruption term
    off it, for anti-sandbox binding. `typeof(game) == 'Instance'` holds in any
    real Roblox execution context (executors included) but not in plain Lua /
    an offline sandbox, where `typeof` is absent — so the decode seed is only
    correct inside Roblox. The `and 0 or WRONG` yields 0 when the check passes
    (0 is truthy in Lua) and WRONG when it fails."""
    wrong = random.randint(1, 16777215)
    # `typeof and ...` short-circuits in plain Lua (typeof is nil) without
    # calling it, so this never errors off Roblox — it just yields WRONG.
    return f" - ((typeof and typeof(game) == 'Instance') and 0 or {wrong})"


def _contains_nested(body):
    """True if the statement list contains any nested function definition
    (anonymous FunctionExpr or `local function`), at any depth. Such a
    function must box its locals so inner closures can capture them."""
    found = [False]

    def walk_expr(e):
        if found[0] or e is None:
            return
        k = e.kind
        if k == "FunctionExpr":
            found[0] = True
            return
        if k == "Paren":
            walk_expr(e.expr)
        elif k == "Index":
            walk_expr(e.obj)
            if not e.dot:
                walk_expr(e.key)
        elif k in ("Call",):
            walk_expr(e.func)
            for a in e.args:
                walk_expr(a)
        elif k == "MethodCall":
            walk_expr(e.obj)
            for a in e.args:
                walk_expr(a)
        elif k == "TableExpr":
            for f in e.fields:
                if f.ftype == "indexed":
                    walk_expr(f.key)
                walk_expr(f.value)
        elif k == "BinOp":
            walk_expr(e.left)
            walk_expr(e.right)
        elif k == "UnOp":
            walk_expr(e.operand)

    def walk_block(stmts):
        for s in stmts:
            if found[0]:
                return
            walk_stmt(s)

    def walk_stmt(s):
        if found[0]:
            return
        k = s.kind
        if k == "LocalFunctionStmt":
            found[0] = True
        elif k == "FunctionDeclStmt":
            found[0] = True
        elif k == "LocalStmt":
            for e in s.exprs:
                walk_expr(e)
        elif k == "AssignStmt":
            for t in s.targets:
                walk_expr(t)
            for e in s.exprs:
                walk_expr(e)
        elif k == "CallStmt":
            walk_expr(s.call)
        elif k == "ReturnStmt":
            for e in s.exprs:
                walk_expr(e)
        elif k == "DoStmt":
            walk_block(s.body)
        elif k == "WhileStmt":
            walk_expr(s.cond)
            walk_block(s.body)
        elif k == "RepeatStmt":
            walk_block(s.body)
            walk_expr(s.cond)
        elif k == "IfStmt":
            for cond, b in s.clauses:
                walk_expr(cond)
                walk_block(b)
            if s.else_body is not None:
                walk_block(s.else_body)
        elif k == "NumericForStmt":
            walk_expr(s.start)
            walk_expr(s.stop)
            if s.step is not None:
                walk_expr(s.step)
            walk_block(s.body)
        elif k == "GenericForStmt":
            for e in s.exprs:
                walk_expr(e)
            walk_block(s.body)

    walk_block(body)
    return found[0]


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
    def __init__(self, junk_prob=0.0, op_names=None, parent=None, boxed=False):
        self.consts = []
        self.const_idx = {}
        self.globals = []
        self.global_idx = {}
        self.regs = {}
        self.code = []  # list of instruction lists, e.g. [op, a, b]
        self.loop_stack = []  # for break patching: list of lists-of-jump-indices
        self.junk_prob = junk_prob
        # closure support
        self.parent = parent          # enclosing FuncCompiler (None at tree root)
        self.boxed = boxed            # whether this function's locals are cells
        self.local_regs = {}          # Lua-local name -> reg (excludes temps)
        self.boxed_regs = set()       # reg indices that hold a cell
        self.upvals = []              # list of (kind, idx): 0=parent local cell, 1=parent upval
        self.upval_names = {}         # upvalue name -> 1-based upval index
        self.children = []            # child FuncCompilers (nested protos), in emission order

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

    # -- Lua-local declaration & name resolution (closure-aware) ----------
    def declare_local(self, name):
        """Allocate a register for an actual Lua local/param and record it as
        a resolvable local (and as a cell if this function is boxed)."""
        r = self.reg(name)
        self.local_regs[name] = r
        if self.boxed:
            self.boxed_regs.add(r)
        return r

    def _resolve_upval(self, name):
        """Return this function's 1-based upvalue index for `name`, allocating
        one (and threading through intermediate frames) if it resolves to an
        enclosing frame's local or upvalue; else None (it's a global)."""
        if name in self.upval_names:
            return self.upval_names[name]
        if self.parent is None:
            return None
        if name in self.parent.local_regs:
            desc = (0, self.parent.local_regs[name])      # capture parent's local cell
        else:
            pu = self.parent._resolve_upval(name)
            if pu is None:
                return None
            desc = (1, pu)                                 # capture parent's upvalue
        idx = len(self.upvals) + 1
        self.upvals.append(desc)
        self.upval_names[name] = idx
        return idx

    def resolve_name(self, name):
        """('local', reg) | ('upval', idx) | ('global', None)."""
        if name in self.local_regs:
            return ("local", self.local_regs[name])
        uv = self._resolve_upval(name)
        if uv is not None:
            return ("upval", uv)
        return ("global", None)

    def push_name(self, name):
        kind, slot = self.resolve_name(name)
        if kind == "local":
            self.emit("PUSHCELL" if slot in self.boxed_regs else "PUSHREG", slot)
        elif kind == "upval":
            self.emit("PUSHUPVAL", slot)
        else:
            self.emit("PUSHGLOBAL", self.g(name))

    def store_name(self, name):
        """Assignment (not declaration): pops the stack top into `name`."""
        kind, slot = self.resolve_name(name)
        if kind == "local":
            self.emit("STORECELL" if slot in self.boxed_regs else "POPREG", slot)
        elif kind == "upval":
            self.emit("STOREUPVAL", slot)
        else:
            # Writing a true global (or a real-Lua local outside the
            # virtualized tree) can't be reflected back: disqualify this tree.
            raise Disqualified()

    def decl_name(self, name):
        """Declaration: pops the stack top as the initial value, creating a
        fresh cell if boxed (so each declaration — including per loop
        iteration — yields a distinct upvalue cell)."""
        self.declare_local(name)
        self._store_fresh(name)

    def _store_fresh(self, name):
        """Pop the stack top into an already-declared local, as a FRESH cell
        when boxed (DECLCELL). Used for loop variables so each iteration binds
        a new cell, matching Lua's per-iteration variable semantics."""
        r = self.local_regs[name]
        self.emit("DECLCELL" if r in self.boxed_regs else "POPREG", r)

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
            self.declare_local(p)
        # If boxed, wrap each already-loaded param value into its own cell so
        # closures can capture it. Done as the first instructions.
        if self.boxed:
            for p in params:
                self.emit("BOXPARAM", self.local_regs[p])
        self.compile_block(body)
        self.emit("RET0")
        # nparams (fixed parameter count) lets the runtime split the packed
        # args into registers + the varargs table.
        self.nparams = len(params)
        return self.code, self.consts, self.globals, self.nparams

    @staticmethod
    def _is_multi(e):
        """True if `e` can yield more than one value in a tail position."""
        return e.kind in ("Call", "MethodCall", "Vararg")

    def _compile_closure(self, params, vararg, body):
        """Compile a nested function into a child proto and emit CLOSURE. The
        child captures this function's cells (or upvalues) by reference, so
        mutations are shared exactly as Lua upvalues are."""
        child = FuncCompiler(junk_prob=self.junk_prob, parent=self,
                             boxed=_contains_nested(body))
        _, _, _, child.nparams = child.compile_function(params, body)
        idx = len(self.children) + 1  # 1-based index into PROTOS
        self.children.append(child)
        self.emit("CLOSURE", idx)

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
                # Compile RHS BEFORE declaring the names, so `local x = x`
                # reads the outer x (Lua scope starts after the statement).
                self._compile_list(s.exprs, len(s.names))  # exactly N values on top
                for nm in reversed(s.names):
                    self.decl_name(nm)  # fresh cell per declaration if boxed
            else:
                for nm in s.names:
                    r = self.declare_local(nm)
                    if r in self.boxed_regs:
                        self.emit("DECLCELLN", r)  # empty cell holding nil
            return
        if k == "LocalFunctionStmt":
            # `local function f` == `local f; f = function...` — f is visible
            # inside its own body (recursion) via its (boxed) cell.
            r = self.declare_local(s.name)
            if r in self.boxed_regs:
                self.emit("DECLCELLN", r)
            self._compile_closure(s.params, s.vararg, s.body)  # pushes the closure
            self.store_name(s.name)                            # store into f's cell
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
            # Declare the loop variable up front so the body resolves it; the
            # fresh-cell DECLCELL (when boxed) is emitted inside the loop so
            # each iteration gets its own cell for closures to capture.
            self.declare_local(s.var)
            top = self.here()  # Lua-1-indexed jump-target VALUE for the back-edge
            forcheck_idx = self.emit("FORCHECK", ctrl, stop_r, step_r, None)  # python idx, for patch()
            self.emit("PUSHREG", ctrl)
            self._store_fresh(s.var)
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
            # The header captures exactly the first 3 values; that's correct
            # only when the last expression doesn't need to expand to fill
            # remaining slots, and no extra (>3) expressions are dropped.
            if len(s.exprs) > 3 or (len(s.exprs) > 1 and self._is_multi(s.exprs[-1])):
                raise Disqualified()
            # Evaluate explist to exactly 3 values (iterator fn, state, ctrl).
            self._compile_explist3(s.exprs)
            reg_c = self.reg(f"__gfc_{id(s)}")
            reg_s = self.reg(f"__gfs_{id(s)}")
            reg_f = self.reg(f"__gff_{id(s)}")
            self.emit("POPREG", reg_c)  # ctrl (top)
            self.emit("POPREG", reg_s)  # state
            self.emit("POPREG", reg_f)  # iterator fn
            nvars = len(s.names)
            # declare loop vars up front (body resolves them); fresh cells are
            # bound each iteration below so closures capture per-iteration vars.
            for nm in s.names:
                self.declare_local(nm)
            key_tmp = self.reg(f"__gfk_{id(s)}")
            top = self.here()
            # call iterator: fn(state, ctrl) -> exactly nvars results
            self.emit("PUSHREG", reg_f)
            self.emit("PUSHREG", reg_s)
            self.emit("PUSHREG", reg_c)
            self.emit("CALLR", 2, nvars)  # 2 args, nvars results on stack (vN on top)
            # bind vN..v2 into fresh cells (reverse), keep v1 (control key) in a temp
            for nm in reversed(s.names[1:]):
                self._store_fresh(nm)
            self.emit("POPREG", key_tmp)  # first result
            # if key is nil -> end loop
            self.emit("PUSHREG", key_tmp)
            jnil = self.emit("JMPNIL", None)
            # not nil: var1 = key (fresh cell), ctrl = key
            self.emit("PUSHREG", key_tmp)
            self._store_fresh(s.names[0])
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
        # ContinueStmt, GotoStmt, LabelStmt, nested FunctionDeclStmt: not
        # supported — fall back to plain Lua for the whole tree.
        raise Disqualified()

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
            self.store_name(t.name)
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
            self.push_name(e.name)
        elif k == "FunctionExpr":
            self._compile_closure(e.params, e.vararg, e.body)
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
    # closures + upvalues (Stage 3): captured locals live in 1-slot cells
    "PUSHCELL",   # push R[a][1]   (read a boxed local)
    "STORECELL",  # R[a][1] = pop  (assign a boxed local)
    "DECLCELL",   # R[a] = {pop}   (declare boxed local = fresh cell w/ value)
    "DECLCELLN",  # R[a] = {}      (declare boxed local = fresh empty cell)
    "BOXPARAM",   # R[a] = {R[a]}  (wrap a loaded param into a cell)
    "PUSHUPVAL",  # push UV[a][1]
    "STOREUPVAL", # UV[a][1] = pop
    "CLOSURE",    # build a closure from PROTOS[a], capturing cells per its upval descriptors
]

# Opcodes whose handler sets `pc` itself; the trailing auto-increment must
# skip them.
_PC_SETTING = {"JMP", "JMPF", "JMPT", "FORCHECK", "JMPNIL"}


def _handler_bodies(_seed, pk, vmrun):
    """Returns {opcode_name: [lua_line, ...]} for the dispatch, with the
    per-build key `K` baked into every operand decode (`I[n]-K`), `pk` the
    name of the value packer, and `vmrun` the interpreter's own name (needed
    by CLOSURE to build nested closures)."""
    g = "I[2]"  # operands are decoded in place by the load-time rolling pass
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
        "FORCHECK":   [f"local _c1,_c2,_c3=R[{g}],R[I[3]],R[I[4]]",
                       "if (_c3>0 and _c1<=_c2) or (_c3<0 and _c1>=_c2) or _c3==0 then pc=pc+1 "
                       f"else pc=I[5] end"],
        "FORSTEP":    [f"R[{g}]=R[{g}]+R[I[3]]"],
        "RET1":       ["return ST[SP]"],
        "RET0":       ["return nil"],
        "NOP":        ["ST[SP]=ST[SP]"],  # harmless; falls through to pc auto-increment
        "CALLR":      [f"local _na=({g})", "local _nr=I[3]", "local _ar={}",
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
        "ADJV":       [f"local _bse=SP-({g})-MC", "local _tgt=_bse+I[3]",
                       "if _tgt>=SP then for _ix=SP+1,_tgt do ST[_ix]=nil end "
                       "else for _ix=_tgt+1,SP do ST[_ix]=nil end end", "SP=_tgt"],
        "SETLISTV":   ["local _tb=ST[SP-MC]",
                       f"for _ix=1,MC do _tb[({g})+_ix-1]=ST[SP-MC+_ix] end",
                       "for _ix=1,MC do ST[SP]=nil SP=SP-1 end"],
        "PUSHCELL":   ["SP=SP+1", f"ST[SP]=R[{g}][1]"],
        "STORECELL":  [f"R[{g}][1]=ST[SP]", "ST[SP]=nil", "SP=SP-1"],
        "DECLCELL":   [f"R[{g}]={{ST[SP]}}", "ST[SP]=nil", "SP=SP-1"],
        "DECLCELLN":  [f"R[{g}]={{}}"],
        "BOXPARAM":   [f"R[{g}]={{R[{g}]}}"],
        "PUSHUPVAL":  ["SP=SP+1", f"ST[SP]=UV[{g}][1]"],
        "STOREUPVAL": [f"UV[{g}][1]=ST[SP]", "ST[SP]=nil", "SP=SP-1"],
        "CLOSURE":    [f"local _pt=PROTOS[{g}]", "local _ds=_pt[5]", "local _uv={}",
                       "for _j=1,#_ds do local _dd=_ds[_j] "
                       "if _dd[1]==0 then _uv[_j]=R[_dd[2]] else _uv[_j]=UV[_dd[2]] end end",
                       "SP=SP+1",
                       f"ST[SP]=function(...) return {vmrun}(_pt[1],_pt[2],_pt[3],_pt[4],{pk}(...),_uv,_pt[6]) end"],
    }


_B64_STD = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _build_runtime(vmrun, pk, decode, op_map, seed, order, b64_alpha, gate=""):
    """Generate the Lua source of the VM interpreter for this build.

    b64_alpha: this build's (shuffled) 64-character base64 alphabet, so the
    blob is not recognizable as standard base64 and a generic decoder fails.

    gate: optional Lua expression appended to the decode accumulator's seed.
    When anti-sandbox is on it evaluates to 0 on real Roblox (so the decode is
    correct) and to a nonzero corruption term off Roblox (so a plain-Lua
    sandbox decodes only garbage and cannot dump the bytecode).

    vmrun:  name of the interpreter function
    pk:     name of the arg/return packer (captures count incl. trailing nils)
    op_map: {opcode_name: random_int}   (the decoded opcode value)
    seed:   per-build seed for the rolling decode accumulator
    order:  opcode names in the (shuffled) order their branches appear

    The 4th VMRUN parameter NP is the function's fixed parameter count: R gets
    R[1..NP] from the packed args AG, and anything past NP forms the varargs
    table VA (with VA.n), so `...` inside the bytecode reads true counts.

    Bytecode is stored under a ROLLING cipher: each number is encoded against a
    running accumulator that folds in every value decoded before it, so the
    array cannot be bulk-decoded statically — an analyzer has to replay the
    exact chained sequence (which differs every build). The whole CODE table is
    decoded in place once, on first entry, then executed normally.
    """
    bodies = _handler_bodies(seed, pk, vmrun)
    # Internal-only names (renamed by the interpreter-hardening pass); not
    # referenced from outside the runtime, so they need not be reserved.
    b64a, b64l = "_B64A", "_B64L"
    branches = []
    for i, name in enumerate(order):
        kw = "if" if i == 0 else "elseif"
        lines = "\n      ".join(bodies[name])
        branches.append(f"    {kw} op == {op_map[name]} then\n      {lines}")
    dispatch = "\n".join(branches) + "\n    end"
    skip = " and ".join(f"op ~= {op_map[n]}" for n in sorted(_PC_SETTING, key=lambda x: op_map[x]))
    return (
        f"local {pk} = function(...) return {{n = select('#', ...), ...}} end\n"
        # Base64 reverse lookup, built once.
        f"local {b64a} = '{b64_alpha}'\n"
        f"local {b64l} = {{}}\n"
        f"for _i = 1, 64 do {b64l}[string.byte({b64a}, _i)] = _i - 1 end\n"
        # Unpack a base64 bytecode blob into the instruction table. The blob is
        # base64-decoded to bytes, then read as: 1-byte slot count, then that
        # many 3-byte big-endian values, every value under the rolling cipher
        # (each depends on all values decoded before it). Runs once per proto
        # at module load, so execution touches plain tables.
        f"local function {decode}(s)\n"
        f"  local B = {{}}\n"
        f"  local bi = 0\n"
        f"  local bt = string.byte\n"
        f"  local ns = #s\n"
        f"  local pd = 0\n"
        f"  if ns > 0 and bt(s, ns) == 61 then pd = pd + 1 end\n"
        f"  if ns > 1 and bt(s, ns - 1) == 61 then pd = pd + 1 end\n"
        f"  local i = 1\n"
        f"  while i <= ns do\n"
        f"    local v = ({b64l}[bt(s, i)] or 0) * 262144 + ({b64l}[bt(s, i + 1)] or 0) * 4096 "
        f"+ ({b64l}[bt(s, i + 2)] or 0) * 64 + ({b64l}[bt(s, i + 3)] or 0)\n"
        f"    i = i + 4\n"
        f"    B[bi + 1] = math.floor(v / 65536) % 256\n"
        f"    B[bi + 2] = math.floor(v / 256) % 256\n"
        f"    B[bi + 3] = v % 256\n"
        f"    bi = bi + 3\n"
        f"  end\n"
        f"  local m = bi - pd\n"
        f"  local C = {{}}\n"
        f"  local p = 1\n"
        f"  local _ac = {seed}{gate}\n"
        f"  local _ic = 0\n"
        f"  while p <= m do\n"
        f"    local cnt = B[p]\n"
        f"    p = p + 1\n"
        f"    _ic = _ic + 1\n"
        f"    local I = {{}}\n"
        f"    for _j = 1, cnt do\n"
        f"      local enc = B[p] * 65536 + B[p + 1] * 256 + B[p + 2]\n"
        f"      p = p + 3\n"
        f"      local _dv = (enc - _ac - _ic - _j) % 16777216\n"
        f"      I[_j] = _dv\n"
        f"      _ac = (_ac * 131 + _dv + _ic) % 16777216\n"
        f"    end\n"
        f"    C[_ic] = I\n"
        f"  end\n"
        f"  return C\n"
        f"end\n"
        f"local function {vmrun}(CODE, KC, GC, NP, AG, UV, PROTOS)\n"
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
        f"    local op = I[1]\n"
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
    def __init__(self, junk_prob=0.25, name_style="confusable", anti_sandbox=False,
                 skip_names=None):
        self.vmrun_name = _random_name()
        self.pk_name = _random_name()
        self.used = False
        self.junk_prob = junk_prob
        self.name_style = name_style  # style for renaming the interpreter internals
        self.anti_sandbox = anti_sandbox
        self.skip_names = set(skip_names or ())  # functions to keep native (hot paths)
        # Constant tables (bytecode + const pools) hoisted out of the per-call
        # path so they're built — and rolling-decoded — exactly once. They all
        # live as entries of ONE container table rather than separate locals:
        # Luau caps a function (the main chunk included) at 200 locals, and a
        # big script has hundreds of vmified protos, which would blow that cap.
        self.hoist_name = _random_name()
        self.hoisted = []  # list of lua_literal (index = position in the container)
        # Per-build instruction set: each opcode gets a distinct random
        # integer, and the branch order is shuffled. Both differ every build.
        nums = random.sample(range(1, 4096), len(_OPCODES))
        self.op_map = dict(zip(_OPCODES, nums))
        self.order = list(_OPCODES)
        random.shuffle(self.order)
        # Per-build seed for the rolling bytecode cipher (see _encode_code).
        self.seed = random.randint(1, 16777215)  # fits the 24-bit rolling cipher
        self.decode_name = _random_name()
        # Per-build shuffled base64 alphabet: the blob reads as random text,
        # not recognizable base64, and a stock base64 decoder won't touch it.
        _a = list(_B64_STD)
        random.shuffle(_a)
        self.b64_alpha = "".join(_a)

    def run(self, chunk):
        chunk.body = self.visit_block(chunk.body, Scope(None))
        if self.used:
            gate = _env_gate() if self.anti_sandbox else ""
            runtime_src = _build_runtime(self.vmrun_name, self.pk_name, self.decode_name,
                                         self.op_map, self.seed, self.order,
                                         self.b64_alpha, gate=gate)
            header = parse(runtime_src).body
            # Harden the interpreter: rename its readable internals (R, ST, op,
            # pc, handlers' temporaries, ...) so the dispatch no longer spells
            # out what each opcode does. The two entry points referenced by the
            # vmified wrappers are kept verbatim.
            from .rename import rename as _rename
            _rename(N("Chunk", body=header), style=self.name_style,
                    reserved={self.vmrun_name, self.pk_name, self.decode_name})
            # Hoisted constant tables: one container local, filled by
            # assignments (statements don't count toward the 200-local cap).
            hoist = []
            if self.hoisted:
                hoist.extend(parse(f"local {self.hoist_name} = {{}}").body)
                for i, lit in enumerate(self.hoisted, 1):
                    hoist.extend(parse(f"{self.hoist_name}[{i}] = {lit}").body)
            chunk.body = header + hoist + chunk.body
        return chunk

    def _hoist(self, lit):
        """Register a constant table literal as an entry of the hoist container
        and return the indexing expression, so it is built/decoded once at
        load rather than per call — without spending a local per proto."""
        self.hoisted.append(lit)
        return f"{self.hoist_name}[{len(self.hoisted)}]"

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
        # Functions the user marked to keep native (hot/per-frame code the VM
        # would make stutter). Marked before renaming, by original name.
        if getattr(node, "skip_vm", False):
            return None
        fc = FuncCompiler(junk_prob=self.junk_prob, parent=None,
                          boxed=_contains_nested(body))
        try:
            code, consts, globs, nparams = fc.compile_function(params, body)
        except Disqualified:
            return None
        except Exception:
            return None
        self.used = True
        # Hoist the two pure-constant tables (bytecode + const pool) to
        # module level so the rolling decode runs once, not every call.
        code_lit = self._hoist(self._encode_code(code))
        const_lit = self._hoist("{" + ",".join(_lua_literal(c) for c in consts) + "}")
        global_lit = "{" + ",".join(globs) + "}"
        # Build the child proto tree as a flat sequence of `local p = {...}`
        # statements (children before parents), not one deeply nested table
        # literal — deep nesting trips Luau's "exceeded recursion depth" on
        # scripts with nested closures. Globals stay inline so they are still
        # captured per call (correct when a global is defined later in the
        # file, or is an outer local the wrapper can see).
        self._proto_decls = []
        child_vars = [self._emit_proto(c) for c in fc.children]
        protos_lit = "{" + ",".join(child_vars) + "}"
        # Rewrite the wrapper to `(...)` and pass ALL incoming args packed (so
        # the true count, trailing nils included, survives). For `:` methods
        # `self` is bound implicitly and captured lexically in globals, so it
        # is not part of `...` and nparams excludes it. UV is empty: a
        # top-level function has no in-tree upvalues (outer references are
        # globals, captured lexically).
        node.params = []
        node.vararg = True
        args_lit = f"{self.pk_name}(...)"
        ret = (f"return {self.vmrun_name}({code_lit}, {const_lit}, {global_lit}, "
               f"{nparams}, {args_lit}, {{}}, {protos_lit})")
        src = "\n".join(self._proto_decls + [ret])
        return parse(src).body

    # -- proto-tree serialization -----------------------------------------
    def _encode_code(self, code):
        """Serialize one proto's bytecode as a single packed STRING (not a
        number array), wrapped in a DECODE(...) call. Each instruction is a
        1-byte slot count followed by its slots; every slot value (opcode
        first, then operands) is encoded under the rolling cipher — against an
        accumulator folding in each value already encoded — and stored as 3
        big-endian bytes. Static tools can't bulk-decode: value N depends on
        1..N-1, the seed/mixing differ each build, and the result reads as one
        opaque string blob rather than thousands of numbers. Must stay in
        lock-step with the DECODE function in _build_runtime."""
        MOD = 16777216  # 2^24 -> 3 bytes per value
        acc = self.seed
        out = []
        for i, ins in enumerate(code, 1):
            vals = [self.op_map[ins[0]]] + [(0 if x is None else x) for x in ins[1:]]
            out.append(chr(len(vals)))
            for j, val in enumerate(vals, 1):
                enc = (val + acc + i + j) % MOD
                out.append(chr((enc >> 16) & 255))
                out.append(chr((enc >> 8) & 255))
                out.append(chr(enc & 255))
                acc = (acc * 131 + val + i) % MOD
        blob = "".join(out)
        # Standard base64, then remap onto this build's shuffled alphabet so
        # the emitted string is not recognizable base64. Padding '=' is kept.
        std = base64.b64encode(blob.encode("latin1")).decode("ascii")
        b64 = std.translate(str.maketrans(_B64_STD, self.b64_alpha))
        return f"{self.decode_name}({lua_string_literal(b64)})"

    def _emit_proto(self, fc):
        """Emit one proto as a flat `local p = {code, consts, globals, nparams,
        upvals, {child vars}}` declaration (appended to self._proto_decls,
        children first) and return its local variable name. The children field
        references child proto LOCALS, not nested literals, so the proto tree
        is a sequence of flat tables rather than one deeply nested expression.
        Bytecode/const pools are hoisted to the module container; globals stay
        inline for correct per-call capture."""
        code_lit = self._hoist(self._encode_code(fc.code))
        const_lit = self._hoist("{" + ",".join(_lua_literal(c) for c in fc.consts) + "}")
        global_lit = "{" + ",".join(fc.globals) + "}"
        upval_lit = "{" + ",".join("{%d,%d}" % (knd, idx) for (knd, idx) in fc.upvals) + "}"
        child_vars = [self._emit_proto(c) for c in fc.children]  # post-order
        children_lit = "{" + ",".join(child_vars) + "}"
        var = _random_name()
        proto_lit = ("{" + code_lit + "," + const_lit + "," + global_lit + ","
                     + str(fc.nparams) + "," + upval_lit + "," + children_lit + "}")
        self._proto_decls.append(f"local {var} = {proto_lit}")
        return var


def vmify(chunk, junk_prob=0.25, name_style="confusable", anti_sandbox=False,
          skip_names=None):
    return VmPass(junk_prob=junk_prob, name_style=name_style,
                  anti_sandbox=anti_sandbox, skip_names=skip_names).run(chunk)
