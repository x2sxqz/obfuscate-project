"""A small tree-walking interpreter over our own AST, used ONLY to verify
(in this test harness) that obfuscation passes preserve behavior. Supports
enough of Lua to run the sample/test scripts: closures & upvalues, tables
with __index metatables, multiple return values & varargs, numeric/generic
for, while/repeat/if, and a tiny stdlib (print, pairs, ipairs, setmetatable,
string.char, string.format-lite, table.concat/insert, tostring, type, #).
"""
import math
import sys
sys.path.insert(0, __file__.rsplit("/", 2)[0])
from lua_obf.parser import parse


class LuaTable:
    def __init__(self):
        self.hash = {}
        self.metatable = None

    def get(self, key):
        key = _normkey(key)
        if key in self.hash:
            return self.hash[key]
        if self.metatable is not None:
            idx = self.metatable.hash.get("__index")
            if isinstance(idx, LuaTable):
                return idx.get(key)
            if callable(idx):
                return idx(self, key)
        return None

    def set(self, key, value):
        key = _normkey(key)
        if value is None:
            self.hash.pop(key, None)
        else:
            self.hash[key] = value

    def length(self):
        n = 0
        while (n + 1) in self.hash:
            n += 1
        return n


def _normkey(k):
    if isinstance(k, float) and k.is_integer():
        return int(k)
    return k


class LuaFunction:
    def __init__(self, params, vararg, body, closure, interp, name="?"):
        self.params = params
        self.vararg = vararg
        self.body = body
        self.closure = closure
        self.interp = interp
        self.name = name

    def __call__(self, *args):
        env = Env(self.closure)
        for i, p in enumerate(self.params):
            env.declare(p, args[i] if i < len(args) else None)
        if self.vararg:
            env.varargs = list(args[len(self.params):])
        try:
            self.interp.exec_block(self.body, env)
        except ReturnSignal as r:
            return tuple(r.values)
        return ()


class Env:
    def __init__(self, parent=None):
        self.vars = {}
        self.parent = parent
        self.varargs = None

    def declare(self, name, value):
        self.vars[name] = value

    def get_varargs(self):
        e = self
        while e is not None:
            if e.varargs is not None:
                return e.varargs
            e = e.parent
        return []

    def lookup_cell(self, name):
        e = self
        while e is not None:
            if name in e.vars:
                return e
            e = e.parent
        return None

    def get(self, name, globals_):
        e = self.lookup_cell(name)
        if e is not None:
            return e.vars[name]
        return globals_.get(name)

    def set(self, name, value, globals_):
        e = self.lookup_cell(name)
        if e is not None:
            e.vars[name] = value
        else:
            globals_.set(name, value)


class BreakSignal(Exception):
    pass


class ContinueSignal(Exception):
    pass


class ReturnSignal(Exception):
    def __init__(self, values):
        self.values = values


def first(v):
    if isinstance(v, tuple):
        return v[0] if v else None
    return v


def truthy(v):
    return v is not None and v is not False


class Interp:
    def __init__(self):
        self.globals = LuaTable()
        self.output = []
        self._install_stdlib()

    def _install_stdlib(self):
        g = self.globals

        def lua_print(*args):
            self.output.append("\t".join(self.tostring(a) for a in args))
            return ()

        def lua_pairs(t):
            items = list(t.hash.items())

            def it(_t, k):
                idx = 0
                if k is not None:
                    for i, (kk, _) in enumerate(items):
                        if kk == k:
                            idx = i + 1
                            break
                else:
                    idx = 0
                if idx >= len(items):
                    return (None,)
                kk, vv = items[idx]
                return (kk, vv)
            return (it, t, None)

        def lua_ipairs(t):
            def it(_t, i):
                i = (i or 0) + 1
                v = t.get(i)
                if v is None:
                    return (None,)
                return (i, v)
            return (it, t, 0)

        def lua_setmetatable(t, mt):
            t.metatable = mt
            return t

        def lua_tostring(v):
            return self.tostring(v)

        def lua_tonumber(v):
            try:
                if isinstance(v, (int, float)):
                    return v
                return float(v) if "." in v else int(v)
            except Exception:
                return None

        def lua_type(v):
            if v is None:
                return "nil"
            if isinstance(v, bool):
                return "boolean"
            if isinstance(v, (int, float)):
                return "number"
            if isinstance(v, str):
                return "string"
            if isinstance(v, LuaTable):
                return "table"
            if callable(v):
                return "function"
            return "userdata"

        g.set("print", lua_print)
        g.set("pairs", lua_pairs)
        g.set("ipairs", lua_ipairs)
        g.set("setmetatable", lua_setmetatable)
        g.set("tostring", lua_tostring)
        g.set("tonumber", lua_tonumber)
        g.set("type", lua_type)

        string_t = LuaTable()

        def s_char(*codes):
            return "".join(chr(int(c)) for c in codes)

        def s_byte(s, i=1, j=None):
            if j is None:
                j = i
            i, j = int(i), int(j)
            if i < 0:
                i = len(s) + i + 1
            if j < 0:
                j = len(s) + j + 1
            return tuple(ord(c) for c in s[i - 1:j])

        string_t.set("char", s_char)
        string_t.set("byte", s_byte)
        g.set("string", string_t)

        table_t = LuaTable()

        def t_concat(t, sep=""):
            n = t.length()
            return sep.join(str(t.get(i)) for i in range(1, n + 1))

        def t_insert(t, a, b=None):
            if b is None:
                t.set(t.length() + 1, a)
            else:
                t.set(a, b)

        table_t.set("concat", t_concat)
        table_t.set("insert", t_insert)
        def t_unpack(t, i=1, j=None):
            if j is None:
                j = t.length()
            return tuple(t.get(k) for k in range(int(i), int(j) + 1))

        table_t.set("unpack", t_unpack)
        g.set("table", table_t)

        math_t = LuaTable()
        math_t.set("floor", math.floor)
        math_t.set("huge", math.inf)
        g.set("math", math_t)

    def tostring(self, v):
        if v is None:
            return "nil"
        if v is True:
            return "true"
        if v is False:
            return "false"
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        if isinstance(v, LuaTable):
            return f"table: 0x{id(v):08x}"
        if callable(v):
            return f"function: 0x{id(v):08x}"
        return str(v)

    # -- running -------------------------------------------------------
    def run(self, chunk):
        env = Env(None)
        env.varargs = []
        try:
            self.exec_block(chunk.body, env)
        except ReturnSignal:
            pass
        return "\n".join(self.output)

    def exec_block(self, stmts, env):
        for s in stmts:
            self.steps = getattr(self, "steps", 0) + 1
            if self.steps > 2_000_000:
                raise RuntimeError("mini_interp: step limit exceeded (likely infinite loop)")
            self.exec_stmt(s, env)

    def exec_stmt(self, s, env):
        k = s.kind
        if k == "LocalStmt":
            vals = self.eval_exprlist(s.exprs, env)
            for i, nm in enumerate(s.names):
                env.declare(nm, vals[i] if i < len(vals) else None)
            return
        if k == "AssignStmt":
            vals = self.eval_exprlist(s.exprs, env)
            setters = [self._make_setter(t, env) for t in s.targets]
            for i, setter in enumerate(setters):
                setter(vals[i] if i < len(vals) else None)
            return
        if k == "CallStmt":
            self.eval_expr_multi(s.call, env)
            return
        if k == "DoStmt":
            self.exec_block(s.body, Env(env))
            return
        if k == "WhileStmt":
            while truthy(self.eval_expr(s.cond, env)):
                try:
                    self.exec_block(s.body, Env(env))
                except BreakSignal:
                    break
                except ContinueSignal:
                    continue
            return
        if k == "RepeatStmt":
            while True:
                inner = Env(env)
                try:
                    self.exec_block(s.body, inner)
                except BreakSignal:
                    break
                except ContinueSignal:
                    pass
                if truthy(self.eval_expr(s.cond, inner)):
                    break
            return
        if k == "IfStmt":
            for cond, body in s.clauses:
                if truthy(self.eval_expr(cond, env)):
                    self.exec_block(body, Env(env))
                    return
            if s.else_body is not None:
                self.exec_block(s.else_body, Env(env))
            return
        if k == "NumericForStmt":
            start = self.eval_expr(s.start, env)
            stop = self.eval_expr(s.stop, env)
            step = self.eval_expr(s.step, env) if s.step is not None else 1
            i = start
            while (step > 0 and i <= stop) or (step < 0 and i >= stop):
                inner = Env(env)
                inner.declare(s.var, i)
                try:
                    self.exec_block(s.body, inner)
                except BreakSignal:
                    break
                except ContinueSignal:
                    pass
                i += step
            return
        if k == "GenericForStmt":
            vals = self.eval_exprlist(s.exprs, env)
            f = vals[0] if len(vals) > 0 else None
            state = vals[1] if len(vals) > 1 else None
            ctrl = vals[2] if len(vals) > 2 else None
            while True:
                res = f(state, ctrl)
                if not isinstance(res, tuple):
                    res = (res,)
                if not res or res[0] is None:
                    break
                ctrl = res[0]
                inner = Env(env)
                for i, nm in enumerate(s.names):
                    inner.declare(nm, res[i] if i < len(res) else None)
                try:
                    self.exec_block(s.body, inner)
                except BreakSignal:
                    break
                except ContinueSignal:
                    continue
            return
        if k == "FunctionDeclStmt":
            params = (["self"] + s.params) if s.is_method else s.params
            fn = LuaFunction(params, s.vararg, s.body, env, self)
            setter = self._make_setter(s.target, env)
            setter(fn)
            return
        if k == "LocalFunctionStmt":
            env.declare(s.name, None)
            fn = LuaFunction(s.params, s.vararg, s.body, env, self, name=s.name)
            env.vars[s.name] = fn
            return
        if k == "ReturnStmt":
            raise ReturnSignal(self.eval_exprlist(s.exprs, env))
        if k == "BreakStmt":
            raise BreakSignal()
        if k == "ContinueStmt":
            raise ContinueSignal()
        if k in ("LabelStmt",):
            return
        if k == "GotoStmt":
            raise NotImplementedError("goto not supported in test interpreter")
        raise ValueError(f"interp: unhandled stmt {k}")

    def _make_setter(self, target, env):
        if target.kind == "Name":
            name = target.name
            def setter(v):
                env.set(name, v, self.globals)
            return setter
        if target.kind == "Index":
            obj = self.eval_expr(target.obj, env)
            key = target.key.value if target.dot else self.eval_expr(target.key, env)
            def setter(v, obj=obj, key=key):
                obj.set(key, v)
            return setter
        raise ValueError("bad assignment target")

    def eval_exprlist(self, exprs, env):
        vals = []
        for i, e in enumerate(exprs):
            if i == len(exprs) - 1:
                r = self.eval_expr_multi(e, env)
                if isinstance(r, tuple):
                    vals.extend(r)
                else:
                    vals.append(r)
            else:
                vals.append(self.eval_expr(e, env))
        return vals

    def eval_expr(self, e, env):
        return first(self.eval_expr_multi(e, env))

    def eval_expr_multi(self, e, env):
        k = e.kind
        if k == "Nil":
            return None
        if k == "True":
            return True
        if k == "False":
            return False
        if k == "Number":
            raw = e.raw
            if raw.lower().startswith("0x"):
                return int(raw, 16)
            if "." in raw or "e" in raw or "E" in raw:
                return float(raw)
            return int(raw)
        if k == "String":
            return e.value
        if k == "Vararg":
            return tuple(env.get_varargs())
        if k == "Name":
            return env.get(e.name, self.globals)
        if k == "Paren":
            return self.eval_expr(e.expr, env)
        if k == "Index":
            obj = self.eval_expr(e.obj, env)
            key = e.key.value if e.dot else self.eval_expr(e.key, env)
            return obj.get(key)
        if k == "Call":
            fn = self.eval_expr(e.func, env)
            args = self.eval_exprlist(e.args, env)
            r = fn(*args)
            return r
        if k == "MethodCall":
            obj = self.eval_expr(e.obj, env)
            fn = obj.get(e.method)
            args = self.eval_exprlist(e.args, env)
            r = fn(obj, *args)
            return r
        if k == "FunctionExpr":
            return LuaFunction(e.params, e.vararg, e.body, env, self)
        if k == "TableExpr":
            t = LuaTable()
            pos = 1
            n = len(e.fields)
            for i, f in enumerate(e.fields):
                if f.ftype == "pos":
                    if i == n - 1:
                        r = self.eval_expr_multi(f.value, env)
                        if isinstance(r, tuple):
                            for v in r:
                                t.set(pos, v)
                                pos += 1
                            continue
                        else:
                            t.set(pos, r)
                    else:
                        t.set(pos, self.eval_expr(f.value, env))
                    pos += 1
                elif f.ftype == "named":
                    t.set(f.key, self.eval_expr(f.value, env))
                else:
                    t.set(self.eval_expr(f.key, env), self.eval_expr(f.value, env))
            return t
        if k == "BinOp":
            return self.eval_binop(e, env)
        if k == "UnOp":
            v = self.eval_expr(e.operand, env)
            if e.op == "-":
                return -v
            if e.op == "not":
                return not truthy(v)
            if e.op == "#":
                if isinstance(v, str):
                    return len(v)
                return v.length()
        raise ValueError(f"interp: unhandled expr {k}")

    def eval_binop(self, e, env):
        op = e.op
        if op == "and":
            l = self.eval_expr(e.left, env)
            return self.eval_expr(e.right, env) if truthy(l) else l
        if op == "or":
            l = self.eval_expr(e.left, env)
            return l if truthy(l) else self.eval_expr(e.right, env)
        l = self.eval_expr(e.left, env)
        r = self.eval_expr(e.right, env)
        if op == "+":
            return l + r
        if op == "-":
            return l - r
        if op == "*":
            return l * r
        if op == "/":
            return l / r
        if op == "%":
            return l - math.floor(l / r) * r
        if op == "^":
            return l ** r
        if op == "..":
            return self.tostring(l) + self.tostring(r)
        if op == "==":
            return self._eq(l, r)
        if op == "~=":
            return not self._eq(l, r)
        if op == "<":
            return l < r
        if op == ">":
            return l > r
        if op == "<=":
            return l <= r
        if op == ">=":
            return l >= r
        raise ValueError(f"interp: unhandled binop {op}")

    def _eq(self, l, r):
        if isinstance(l, (int, float)) and isinstance(r, (int, float)):
            return l == r
        return l is r if isinstance(l, LuaTable) or isinstance(r, LuaTable) else l == r


def run_source(src):
    ast = parse(src)
    return Interp().run(ast)
