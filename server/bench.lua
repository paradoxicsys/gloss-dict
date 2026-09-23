-- wrk benchmark script: picks a random real dictionary key per request,
-- so the benchmark exercises a realistic spread across the index/mmap
-- rather than hammering one hot cache line.
local words = {}
for line in io.lines("bench_words.txt") do
  if #line > 0 then
    table.insert(words, line)
  end
end

math.randomseed(42)

request = function()
  local w = words[math.random(#words)]
  return wrk.format("GET", "/v1/define/" .. w)
end
