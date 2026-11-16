-- closures sharing mutable upvalues
local function makeCounter()
  local n = 0
  return function()
    n = n + 1
    return n
  end
end
local c1 = makeCounter()
local c2 = makeCounter()
print(c1(), c1(), c1(), c2(), c1())   -- 1 2 3 1 4

-- two closures sharing ONE cell
local function makePair()
  local v = 0
  local get = function() return v end
  local set = function(x) v = x end
  return get, set
end
local get, set = makePair()
set(42)
print(get())                          -- 42
set(get() + 8)
print(get())                          -- 50

-- fresh cell per loop iteration
local function makeFns()
  local fns = {}
  for i = 1, 3 do
    fns[i] = function() return i end
  end
  return fns
end
local fns = makeFns()
print(fns[1](), fns[2](), fns[3]())   -- 1 2 3

-- nested two levels, upvalue chain
local function outer(a)
  local function middle(b)
    local function inner(c)
      return a + b + c
    end
    return inner
  end
  return middle
end
print(outer(100)(20)(3))              -- 123

-- closure capturing a param and mutating it
local function adder(start)
  return function(d)
    start = start + d
    return start
  end
end
local add = adder(10)
print(add(5), add(5), add(90))        -- 15 20 110

-- closure over a generic-for loop var + accumulator
local function collect(t)
  local out = {}
  local idx = 0
  for _, v in ipairs(t) do
    idx = idx + 1
    out[idx] = function() return v * 2 end
  end
  return out
end
local fs = collect({3, 5, 7})
print(fs[1](), fs[2](), fs[3]())      -- 6 10 14
