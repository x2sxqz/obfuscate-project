import sys, os, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lua_obf.parser import parse
from lua_obf.codegen import CodeGen
from lua_obf.transforms.rename import rename
from lua_obf.transforms.strings import encrypt_strings
from lua_obf.transforms.numbers import obfuscate_numbers
from lua_obf.transforms.cff import flatten_control_flow
from lua_obf.transforms.vm import vmify
from mini_interp import run_source

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "sample2.lua")
src = open(path, encoding="utf-8").read()

print("=== running ORIGINAL ===")
orig_out = run_source(src)
print(orig_out)

seed = int(sys.argv[2]) if len(sys.argv) > 2 else 123
random.seed(seed)

ast = parse(src)
rename(ast, style="hex")
encrypt_strings(ast)
obfuscate_numbers(ast, probability=0.9, max_depth=2)
vmify(ast)
flatten_control_flow(ast, probability=0.8)
obf_src = CodeGen(minify=False).gen_chunk(ast)

obf_path = "/tmp/obf_out.lua"
with open(obf_path, "w") as f:
    f.write(obf_src)
print(f"\n(obfuscated source written to {obf_path}, {len(obf_src)} bytes)")

print("\n=== running OBFUSCATED ===")
obf_out = run_source(obf_src)
print(obf_out)

print("\n=== DIFF ===")
if orig_out == obf_out:
    print("MATCH: outputs are identical.")
else:
    import difflib
    for line in difflib.unified_diff(orig_out.splitlines(), obf_out.splitlines(), lineterm=""):
        print(line)
    raise SystemExit("MISMATCH")
