import sys, os, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lua_obf.parser import parse
from lua_obf.codegen import CodeGen
from lua_obf.transforms.numbers import obfuscate_numbers

random.seed(7)
path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "sample1.lua")
src = open(path, encoding="utf-8").read()

ast = parse(src)
obfuscate_numbers(ast, probability=1.0, max_depth=3)
out = CodeGen(minify=False).gen_chunk(ast)
print(out)
ast2 = parse(out)
print("---- OK: number-obfuscated code re-parses ----")
