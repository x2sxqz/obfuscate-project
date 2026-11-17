local function top()
  local v0 = 0
  return function()
    local v1 = 1
    return function()
      local v2 = 2
      return function()
        local v3 = 3
        return function()
          local v4 = 4
          return function()
            local v5 = 5
            return function()
              local v6 = 6
              return function()
                local v7 = 7
                return function()
                  local v8 = 8
                  return function()
                    local v9 = 9
                    return function()
                      local v10 = 10
                      return function()
                        local v11 = 11
                        return function()
                          return v0+v1+v2+v3+v4+v5+v6+v7+v8+v9+v10+v11
                        end
                      end
                    end
                  end
                end
              end
            end
          end
        end
      end
    end
  end
end
local f = top()
print(f()()()()()()()()()()()())
