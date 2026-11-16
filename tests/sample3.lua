-- continue / nested loops / labels edge cases
local total = 0
for i = 1, 10 do
    if i % 3 == 0 then
        continue
    end
    total = total + i
end
print("total", total)

local found = nil
for i = 1, 5 do
    for j = 1, 5 do
        if i * j == 12 then
            found = {i, j}
            break
        end
    end
    if found then
        break
    end
end
print("found", found[1], found[2])

do
    local x = 1
    do
        local y = 2
        print("nested do", x, y)
    end
end

local function multi()
    return 1, 2, 3
end
local a, b, c = multi()
print(a, b, c)

local t = {}
for i = 1, 5 do
    t[i] = function() return i * i end
end
print(t[1](), t[2](), t[3](), t[4](), t[5]())

local s = ""
for i = 1, 3 do
    s = s .. tostring(i)
end
print(s)
