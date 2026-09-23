#!/usr/bin/env python3
"""Look up one or more words against a built dict.idx/dict.bin, exactly as
the Phase 3 server will (normalize -> hashmap lookup -> read bytes -> gunzip),
and pretty-print the result. Used for the Phase 2 spot-check list.

Usage:
    python3 dict-api/build/spotcheck.py --out-dir OUT_DIR word [word ...]
"""
import argparse
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.normalize import normalize  # noqa: E402
from common.idx_format import read_idx  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("words", nargs="+")
    args = ap.parse_args()

    idx_path = os.path.join(args.out_dir, "dict.idx")
    bin_path = os.path.join(args.out_dir, "dict.bin")

    build_version, index = read_idx(idx_path)
    print(f"build_version={build_version}  keys={len(index):,}\n")

    with open(bin_path, "rb") as bin_f:
        for raw_word in args.words:
            key = normalize(raw_word)
            print(f"--- {raw_word!r} -> key {key!r} ---")
            if key not in index:
                print("  NOT FOUND\n")
                continue
            offset, length = index[key]
            bin_f.seek(offset)
            gz = bin_f.read(length)
            raw = gzip.decompress(gz)
            payload = json.loads(raw)
            print(f"  stored bytes: {length:,} gzip / {len(raw):,} raw")
            for result in payload["results"]:
                via = f"  [via: {result['via']}]" if "via" in result else ""
                ipa = f" {result['ipa']}" if "ipa" in result else ""
                audio = " (audio)" if "audio" in result else ""
                print(f"  word={result['word']!r}{ipa}{audio}{via}")
                for entry in result["entries"]:
                    trunc = " [truncated]" if entry.get("truncated") else ""
                    print(f"    pos={entry['pos']}{trunc}")
                    for sense in entry["senses"]:
                        tags = f"  tags={sense['tags']}" if "tags" in sense else ""
                        print(f"      - {sense['def']}{tags}")
            print()


if __name__ == "__main__":
    main()
