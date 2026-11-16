local function minmax(t)
  local lo, hi = t[1], t[1]
  for i, v in ipairs(t) do
    if v < lo then lo = v end
    if v > hi then hi = v end
  end
  return lo, hi
end

local function sum(...)
  local args = {...}
  local s = 0
  for i, v in ipairs(args) do s = s + v end
  return s
end

local function count(...)
  return select("#", ...)
end

local function first3(...)
  local a, b, c = ...
  return a, b, c
end

local function wrap(...)
  return sum(...), count(...)
end

local lo, hi = minmax({5, 2, 9, 1, 7})
print(lo, hi)
print(sum(1, 2, 3, 4, 5))
print(count(10, 20, 30))
print(first3(100, 200, 300, 400))
print(wrap(1, 2, 3))

local function makePair() return 11, 22 end
local x, y = makePair()
print(x, y)
local t = {makePair()}
print(t[1], t[2], #t)
print(makePair())
