#!/usr/bin/env bash
# Post-deployment smoke test against the live CloudFront URL. Mirrors the
# same checks run locally in Phase 3, but against the real deployed path:
# viewer -> CloudFront -> EC2 -> mmap'd dict.bin.
#
# Usage: ./smoke_test.sh <cloudfront-domain>
#   e.g. ./smoke_test.sh d111111abcdef8.cloudfront.net
set -uo pipefail

HOST="${1:?Usage: ./smoke_test.sh <cloudfront-domain>}"
BASE="https://$HOST"
PASS=0
FAIL=0

check() {
  local desc="$1" got="$2" want="$3"
  if [ "$got" = "$want" ]; then
    echo "  OK   $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL $desc -- got '$got', want '$want'"
    FAIL=$((FAIL + 1))
  fi
}

echo "== $BASE =="

echo "-- health --"
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/health")
check "GET /health -> 200" "$code" "200"

echo "-- viewer protocol: HTTP redirects to HTTPS --"
code=$(curl -s -o /dev/null -w "%{http_code}" "http://$HOST/health")
if [ "$code" = "301" ] || [ "$code" = "302" ]; then
  echo "  OK   GET http://.../health -> $code (redirect to HTTPS)"
  PASS=$((PASS + 1))
else
  echo "  FAIL GET http://.../health -> $code, want 301 or 302"
  FAIL=$((FAIL + 1))
fi

echo "-- found word, headers --"
headers=$(curl -sI "$BASE/v1/define/went")
code=$(echo "$headers" | head -1 | grep -o '[0-9][0-9][0-9]')
check "GET /v1/define/went -> 200" "$code" "200"
enc=$(echo "$headers" | grep -i '^content-encoding:' | tr -d '\r' | awk '{print $2}')
check "Content-Encoding" "$enc" "gzip"
cache=$(echo "$headers" | grep -i '^cache-control:' | tr -d '\r' | cut -d' ' -f2-)
check "Cache-Control" "$cache" "public, max-age=604800"
vary=$(echo "$headers" | grep -i '^vary:' | tr -d '\r' | awk '{print $2}')
check "Vary" "$vary" "Accept-Encoding"

echo "-- body content (via CloudFront's own gzip handling) --"
body=$(curl -s "$BASE/v1/define/went" --compressed)
query=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin)['query'])" 2>/dev/null || echo "PARSE_ERROR")
check "query field" "$query" "went"
has_go_via=$(echo "$body" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('yes' if any(r.get('via') == 'simple past of go' and r['word'] == 'go' for r in d['results']) else 'no')
" 2>/dev/null || echo "PARSE_ERROR")
check "went -> go via link present" "$has_go_via" "yes"

echo "-- 404 --"
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/v1/define/zzzznotarealword")
check "GET /v1/define/zzzznotarealword -> 404" "$code" "404"

echo "-- 400 --"
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/v1/define/123")
check "GET /v1/define/123 -> 400" "$code" "400"

echo "-- 405 (or 403 via CloudFront, which restricts methods at the edge) --"
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/v1/define/cat")
if [ "$code" = "405" ] || [ "$code" = "403" ]; then
  echo "  OK   POST /v1/define/cat -> $code (method rejected)"
  PASS=$((PASS + 1))
else
  echo "  FAIL POST /v1/define/cat -> $code, want 405 or 403"
  FAIL=$((FAIL + 1))
fi

echo "-- conditional GET --"
etag=$(curl -sI "$BASE/v1/define/cat" | grep -i '^etag:' | tr -d '\r' | cut -d' ' -f2-)
code=$(curl -s -o /dev/null -w "%{http_code}" -H "If-None-Match: $etag" "$BASE/v1/define/cat")
check "GET with matching If-None-Match -> 304" "$code" "304"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
