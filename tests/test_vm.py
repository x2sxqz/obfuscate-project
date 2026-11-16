import sys, os, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lua_obf.parser import parse
from lua_obf.codegen import CodeGen
from lua_obf.transforms.vm import vmify
from mini_interp import run_source

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "sample_vm.lua")
src = open(path, encoding="utf-8").read()

print("=== ORIGINAL ===")
orig_out = run_source(src)
print(orig_out)

seed = int(sys.argv[2]) if len(sys.argv) > 2 else 99
random.seed(seed)

ast = parse(src)
vmify(ast)
obf_src = CodeGen(minify=False).gen_chunk(ast)
with open("/tmp/vm_out.lua", "w") as f:
    f.write(obf_src)
print(f"\n(VM output written to /tmp/vm_out.lua, {len(obf_src)} bytes)")

print("\n=== VM-IFIED ===")
obf_out = run_source(obf_src)
print(obf_out)

print("\n=== DIFF ===")
if orig_out == obf_out:
    print("MATCH")
else:
    import difflib
    for line in difflib.unified_diff(orig_out.splitlines(), obf_out.splitlines(), lineterm=""):
        print(line)
    raise SystemExit("MISMATCH")
