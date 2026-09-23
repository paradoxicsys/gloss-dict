# Dictionary API

Click-to-define dictionary API for a test-taking platform. A student clicks a word in a
reading passage; the API returns its definitions, part of speech, pronunciation (IPA + audio),
examples, synonyms/antonyms, and word forms — built on the [kaikki.org](https://kaikki.org)
English Wiktionary extract (~1.57M lookup keys).

Every response is pre-built offline. Each request is one normalized hashmap lookup plus a byte
copy from a memory-mapped file — no database, no per-request JSON parsing or compression.

## Architecture

```
3 GB JSONL (kaikki.org)  ──build──▶  dict.idx + dict.bin (~557 MB)
                                            │
                                   S3 (builds/<version>/, CURRENT pointer)
                                            │
                                  EC2 (Go server: hashmap + mmap)
                                            │
                                   CloudFront (HTTPS, caching)
                                            │
                                     Student's browser
```

- **Build time**: normalize headwords, resolve word-form links (plurals, past tense, alternate
  spellings, etc. → their lemma), drop vulgar/offensive senses and dialectal/archaic form-of
  traps (e.g. `book` is not shown as a dialectal past tense of `bake`), trim each entry to what a
  student needs, gzip-compress, write a binary index.
- **Request time**: normalize the clicked word → one hashmap lookup → read bytes from the
  memory-mapped data file → send as-is with `Content-Encoding: gzip`. No JSON parsing or
  compression per request, except a rare fallback for clients that don't accept gzip.

## API

`GET /v1/define/{word}` — `{word}` is exactly what the student clicked, URL-encoded (e.g.
`Running%2C`). Returns the word's own meanings plus every lemma it's a known form of — e.g.
`went` returns both its own entry and `go`, tagged `"via": "simple past of go"`.

`GET /health` — for the load balancer / CDN.

Full schema: [`openapi.yaml`](openapi.yaml).

## Repository layout

- `common/` — normalization and payload-trimming logic. Implemented once in Python (used by
  `explore/` and `build/`) and mirrored line-for-line in Go (`server/normalize.go`), so a key
  computed at build time always matches the key computed for an incoming request.
- `explore/` — streaming exploration of the raw kaikki.org JSONL (entry counts, field-size
  breakdown, form-resolution stats, sense-tag frequency, output-size estimate).
- `build/` — the offline build: JSONL → `dict.idx` (binary key index) + `dict.bin`
  (gzip-compressed payloads), plus a spot-check tool for verifying specific words.
- `server/` — the Go server: loads the index into memory, mmaps the data file, serves lookups
  over a hand-rolled connection loop (single-write responses, `TCP_NODELAY`) with a full test
  suite.
- `deploy/` — AWS deployment: S3 layout, IAM policy, EC2 user-data/systemd unit, a CloudFront
  setup runbook, and scripts to publish new builds, roll back, and smoke-test a live deployment.
- `openapi.yaml` — API specification.

## Building

Requires the raw kaikki.org English Wiktionary JSONL extract (not included in this repo, ~3 GB —
download from kaikki.org).

```bash
python3 build/build.py /path/to/kaikki-English.jsonl --out-dir out
```

Produces `out/dict.idx` and `out/dict.bin`.

## Running locally

```bash
cd server
go build -o dictapi-server .
DICT_DIR=../out PORT=8080 ALLOW_ORIGIN='*' ./dictapi-server
```

## Testing

```bash
cd server
go test ./...
```


## License

Dictionary data: Wiktionary via kaikki.org, [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
