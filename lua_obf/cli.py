#!/usr/bin/env python3
"""Command-line entry point for the Lua obfuscator."""

import argparse
import random
import sys
import time

from .parser import parse, ParseError
from .codegen import CodeGen
from .transforms.rename import rename
from .transforms.strings import encrypt_strings
from .transforms.numbers import obfuscate_numbers
from .transforms.cff import flatten_control_flow
from .transforms.vm import vmify

PRESETS = {
    "minify":  {"rename": False, "strings": False, "numbers": False, "vm": False, "cff": False},
    "light":   {"rename": True,  "strings": False, "numbers": False, "vm": False, "cff": False},
    "medium":  {"rename": True,  "strings": True,  "numbers": True,  "vm": False, "cff": False},
    "heavy":   {"rename": True,  "strings": True,  "numbers": True,  "vm": False, "cff": True},
    "max":     {"rename": True,  "strings": True,  "numbers": True,  "vm": True,  "cff": True},
}


def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="lua-obf",
        description="Obfuscate Lua/Luau scripts: identifier renaming, string "
                    "encryption, numeric-literal obfuscation, control-flow "
                    "flattening, and an optional bytecode-VM mode for closed "
                    "pure functions.",
    )
    p.add_argument("input", help="input .lua file")
    p.add_argument("-o", "--output", help="output file (default: <input>.obf.lua)")
    p.add_argument(
        "--preset", choices=list(PRESETS.keys()), default="heavy",
        help="minify (no obfuscation, just compact) / light (rename only) / "
             "medium (+ string & number obfuscation) / heavy (+ control-flow "
             "flattening, default) / max (+ bytecode VM for eligible functions)",
    )
    p.add_argument("--minify", action="store_true", help="collapse the whole output onto a single line")
    p.add_argument("--banner", default=None, metavar="TEXT",
                    help="prepend a top-of-file comment, e.g. --banner \"This file was protected by REAPER HUB\"")
    p.add_argument("--name-style", choices=["confusable", "hex", "mixed"], default="mixed",
                    help="mixed: highly varied a-z/A-Z/0-9/_ names (default). confusable: "
                         "look-alike l/I/1/0/O/o/_ names (hardest to read, but monotonous). "
                         "hex: _0x-style names, easiest to debug your own output.")
    p.add_argument("--seed", type=int, default=None, help="random seed, for reproducible output")

    # individual toggles — override whatever the preset picked
    p.add_argument("--rename", dest="rename", action="store_true", default=None)
    p.add_argument("--no-rename", dest="rename", action="store_false")
    p.add_argument("--strings", dest="strings", action="store_true", default=None)
    p.add_argument("--no-strings", dest="strings", action="store_false")
    p.add_argument("--numbers", dest="numbers", action="store_true", default=None)
    p.add_argument("--no-numbers", dest="numbers", action="store_false")
    p.add_argument("--cff", dest="cff", action="store_true", default=None)
    p.add_argument("--no-cff", dest="cff", action="store_false")
    p.add_argument("--vm", dest="vm", action="store_true", default=None)
    p.add_argument("--no-vm", dest="vm", action="store_false")

    p.add_argument("--cff-probability", type=float, default=0.6,
                    help="0..1 chance an eligible block gets control-flow-flattened (default 0.6)")
    p.add_argument("--number-probability", type=float, default=0.85,
                    help="0..1 chance an eligible integer literal gets arithmetic-obfuscated (default 0.85)")
    p.add_argument("--vm-junk", type=float, default=0.25,
                    help="0..1 rate of junk NOP instructions sprinkled into VM bytecode (default 0.25)")
    p.add_argument("--anti-sandbox", action="store_true",
                    help="bind string + bytecode decoding to the Roblox environment "
                         "(typeof(game)=='Instance'); run outside Roblox and it decodes to "
                         "garbage, so it can't be dumped in a plain-Lua sandbox. "
                         "WARNING: makes the output Roblox-only — don't use for generic Lua.")

    p.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    return p


def run(argv=None):
    args = build_arg_parser().parse_args(argv)

    preset = dict(PRESETS[args.preset])
    for key in ("rename", "strings", "numbers", "cff", "vm"):
        override = getattr(args, key)
        if override is not None:
            preset[key] = override

    if args.seed is not None:
        random.seed(args.seed)

    try:
        with open(args.input, "r", encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        print(f"error: can't read {args.input}: {e}", file=sys.stderr)
        return 1

    t0 = time.time()
    try:
        ast = parse(src)
    except ParseError as e:
        print(f"error: failed to parse {args.input}: {e}", file=sys.stderr)
        return 1

    steps = []
    if preset["rename"]:
        rename(ast, style=args.name_style)
        steps.append("rename")
    if preset["strings"]:
        encrypt_strings(ast, anti_sandbox=args.anti_sandbox)
        steps.append("strings")
    if preset["numbers"]:
        obfuscate_numbers(ast, probability=args.number_probability)
        steps.append("numbers")
    if preset["vm"]:
        vmify(ast, junk_prob=args.vm_junk, name_style=args.name_style,
              anti_sandbox=args.anti_sandbox)
        steps.append("vm")
    if preset["cff"]:
        flatten_control_flow(ast, probability=args.cff_probability)
        steps.append("cff")

    out_src = CodeGen(minify=args.minify).gen_chunk(ast)

    # sanity check: make sure our own output re-parses before handing it
    # back — if it doesn't, something in the pipeline produced broken Lua
    # and we should say so loudly rather than silently ship it.
    try:
        parse(out_src)
    except ParseError as e:
        print(f"internal error: generated output failed to re-parse ({e}). "
              f"Please report this — nothing was written.", file=sys.stderr)
        return 2

    if args.banner:
        # One comment line per line of banner text, then the code. A Lua `--`
        # comment runs to end of line, so each needs its own line.
        banner = "\n".join("-- " + line for line in args.banner.split("\\n"))
        out_src = banner + "\n" + out_src

    out_path = args.output or _default_output_path(args.input)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out_src)

    if not args.quiet:
        dt = time.time() - t0
        pct = (100.0 * len(out_src) / max(1, len(src)))
        print(f"wrote {out_path}  ({len(src)} -> {len(out_src)} bytes, {pct:.0f}%)  "
              f"[{', '.join(steps) or 'no transforms'}]  {dt:.2f}s")
    return 0


def _default_output_path(input_path):
    if input_path.endswith(".lua"):
        return input_path[: -len(".lua")] + ".obf.lua"
    return input_path + ".obf.lua"


def main():
    raise SystemExit(run())


if __name__ == "__main__":
    main()
