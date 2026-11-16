local function sumArray(t)
  local s = 0
  for i, v in ipairs(t) do
    s = s + v
  end
  return s
end

local function countKeys(t)
  local n = 0
  for k, v in pairs(t) do
    n = n + 1
  end
  return n
end

local function joinVals(t)
  local out = ""
  for i, v in ipairs(t) do
    out = out .. tostring(v) .. ","
  end
  return out
end

local function findFirst(t, target)
  for i, v in ipairs(t) do
    if v == target then
      return i
    end
  end
  return -1
end

print(sumArray({10, 20, 30, 40}))
print(countKeys({a=1, b=2, c=3}))
print(joinVals({"x", "y", "z"}))
print(findFirst({5, 8, 13, 21}, 13))
print(findFirst({1, 2, 3}, 99))
