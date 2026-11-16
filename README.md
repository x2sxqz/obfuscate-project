# lua-obf

A standalone Lua/Luau obfuscator — written from scratch (own lexer, parser,
code generator) as a CLI tool. Built for scripts run through an executor
(Roblox or otherwise): no dependencies beyond Python 3 (3.8+).

## Install

**Straight from GitHub, one command**, once this is pushed to a repo of
yours:

```
pip install git+https://github.com/<you>/<repo>.git
```

This installs a `lua-obf` command on your `PATH` (via the `[project.scripts]`
entry point in `pyproject.toml`) — no manual `PYTHONPATH`/`cd` needed
afterward:

```
lua-obf script.lua --preset max -o script.obf.lua
```

To update after you push changes: `pip install --upgrade --force-reinstall
git+https://github.com/<you>/<repo>.git`.

**From a local clone** (editable install — picks up edits without
reinstalling):

```
git clone https://github.com/<you>/<repo>.git
cd <repo>
pip install -e .
```

**Without installing at all** (just run it from the checkout):

```
python3 -m lua_obf input.lua [-o output.lua] [--preset heavy] [--seed 1234]
```

On Termux, `pkg install python` first, then either of the `pip install`
forms above works the same way (pure Python, nothing to compile).

### Presets (`--preset`)

| preset   | rename | string encrypt | number obfuscate | control-flow flatten | bytecode VM |
|----------|:------:|:---------------:|:-----------------:|:---------------------:|:------------:|
| minify   |        |                 |                    |                        |              |
| light    | ✓      |                 |                    |                        |              |
| medium   | ✓      | ✓               | ✓                  |                        |              |
| heavy *(default)* | ✓ | ✓          | ✓                  | ✓                      |              |
| max      | ✓      | ✓               | ✓                  | ✓                      | ✓            |

Any pass can also be forced on/off individually regardless of preset:
`--rename/--no-rename`, `--strings/--no-strings`, `--numbers/--no-numbers`,
`--cff/--no-cff`, `--vm/--no-vm`. Add `--minify` to also strip layout
whitespace from the final output.

### What each pass does

- **rename** — every local variable, parameter, and `local function` gets a
  random name (confusable `l`/`I` characters by default, or `--name-style
  hex` for `_0x...`-style names you can still eyeball while testing). Globals
  (`print`, `game`, `workspace`, `string`, ...) and table/field keys are
  never touched, since renaming those would change behavior.
- **strings** — every string literal is pulled into a byte table, encrypted
  with a per-string salt and a per-build key, and replaced with a lookup
  into a table that gets decoded once at script start.
- **numbers** — integer literals get replaced with an equivalent small
  arithmetic expression tree (`10` → `((3*7)-11)`, etc).
- **cff** (control-flow flattening) — turns a straight-line sequence of
  statements into a `while state ~= 0 do if state == id then ... end end`
  dispatcher, with the branches written out in shuffled order and jumping
  between randomized state ids. Only applied where it's provably safe (see
  the big comment at the top of `lua_obf/transforms/cff.py` for the exact
  rules — short version: it never touches a loop's own body, so `break`
  always keeps meaning what it already meant).
- **vm** — compiles eligible functions to a small custom bytecode format,
  executed by an interpreter loop embedded in the output. This is the
  strongest and most restrictive pass: a function only qualifies if it's
  "closed" (no nested closures, no captured-and-*written* upvalues — reads
  are fine) and free of multi-return ambiguity. Anything that doesn't
  qualify is silently left as plain (but still renamed/encrypted/flattened)
  Lua — this pass never ships a function it can't fully verify. See the
  docstring in `lua_obf/transforms/vm.py` for the full eligibility rules.

## How it's built

`lua_obf/lexer.py` and `lua_obf/parser.py` implement a Lua 5.1 / Luau-subset
grammar from scratch (long strings/comments, hex/scientific numbers, Luau's
`continue` and `<const>`-style attributes, compound assignment operators).
`lua_obf/codegen.py` turns the AST back into source. Every transform in
`lua_obf/transforms/` operates on that same AST.

## Testing

There's no Lua interpreter available in this environment, so correctness is
checked with a small tree-walking Lua interpreter (`tests/mini_interp.py`,
test-only — not shipped) that runs the *same* AST our parser produces. Each
test diffs a script's original output against its fully-obfuscated output:

```
python3 tests/test_pipeline_diff.py tests/sample_vm.lua 1     # one seed
```

Run `tests/*.py` across a range of `--seed` values before trusting a change
to the pipeline — randomized passes (cff, numbers, vm) can behave
differently per seed. If you do have a real Lua/Luau interpreter available,
sanity-check the output there too — the mini interpreter is a useful
cross-check but isn't a substitute for the real thing.

## Known limitations

- Targets Lua 5.1-ish syntax plus common Luau extras. No bitwise operators
  (`&`, `|`, `~` as binary, `<<`, `>>`), no string interpolation (`` `{}` ``),
  no typed syntax (`--!strict` annotations are just comments to us — fine,
  since we don't need to type-check, only parse and re-emit).
- `goto`/`::label::` are parsed and passed through unmodified by every pass,
  but control-flow flattening and VM mode both deliberately refuse to touch
  a block that contains one.
- The VM pass's eligibility bar is intentionally strict; most real-world
  functions with side effects (writing to tables/globals, calling nested
  closures) won't qualify and will just go through the other passes
  instead. That's by design — a half-correct VM is worse than no VM.
