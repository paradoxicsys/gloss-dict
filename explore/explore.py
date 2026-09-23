#!/usr/bin/env python3
"""Phase 1: explore the kaikki.org English Wiktionary JSONL extract.

Single streaming pass over the file (no full-file load). Produces a
6-section report:
  1. Entry counts
  2. Field size breakdown (whole-file Nth-line sample)
  3. Form resolution stats (form_of/alt_of + forms[], post-filter, unique keys)
  4. Sense tag frequency
  5. Estimated build output size (APPROXIMATE, labeled as such)
  6. Key-normalization stats (unique keys, case collisions, IPA/audio coverage)

Usage:
    python3 dict-api/explore/explore.py [path-to-jsonl]
"""
import argparse
import gzip
import json
import os
import resource
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.normalize import normalize, is_valid_key  # noqa: E402
from common.payload import (  # noqa: E402
    FORM_LINK_DROP_TAGS,
    build_line_payload,
    dumps_compact,
)

DEFAULT_PATH = "/Users/shambhuyadav/Desktop/kaikki.org-dictionary-English.jsonl"

SAMPLE_EVERY = 30  # every Nth line, uniformly across the WHOLE file
PROGRESS_EVERY = 200_000


# --------------------------------------------------------------------------
# Main streaming pass
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?", default=DEFAULT_PATH)
    args = ap.parse_args()

    t0 = time.time()

    total_lines = 0
    english_lines = 0
    malformed_lines = 0
    words_seen = set()
    word_pos_seen = set()

    key_to_word = {}       # normalized key -> first original word seen
    collision_keys = set()  # keys with >1 distinct original casing

    tag_counter = Counter()
    senses_with_form_of = 0
    senses_with_alt_of = 0
    form_links_kept = 0
    form_links_dropped = 0
    forms_arr_kept = 0
    forms_arr_dropped = 0
    form_keys_from_formof = set()
    form_keys_from_forms_arr = set()

    ipa_entries = 0
    audio_entries = 0

    field_bytes = Counter()
    sample_gz_bytes = 0
    sample_count = 0

    print(f"Streaming {args.path} ...", file=sys.stderr)

    with open(args.path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            total_lines += 1
            if line_no % PROGRESS_EVERY == 0:
                elapsed = time.time() - t0
                print(f"... {line_no:,} lines ({elapsed:.0f}s elapsed)", file=sys.stderr)

            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                malformed_lines += 1
                continue

            if d.get("lang_code") != "en":
                continue
            english_lines += 1

            word = d.get("word", "")
            pos = d.get("pos", "")
            words_seen.add(word)
            word_pos_seen.add((word, pos))

            key = normalize(word)
            key_valid = is_valid_key(key)
            if key_valid:
                if key in key_to_word:
                    if key_to_word[key] != word:
                        collision_keys.add(key)
                else:
                    key_to_word[key] = word

            sounds = d.get("sounds", []) or []
            if any(s.get("ipa") for s in sounds):
                ipa_entries += 1
            if any(s.get("mp3_url") for s in sounds):
                audio_entries += 1

            for s in d.get("senses", []) or []:
                stags = set(s.get("tags", []) or [])
                tag_counter.update(stags)

                has_form_of = bool(s.get("form_of"))
                has_alt_of = bool(s.get("alt_of"))
                if has_form_of:
                    senses_with_form_of += 1
                if has_alt_of:
                    senses_with_alt_of += 1
                if has_form_of or has_alt_of:
                    if stags & FORM_LINK_DROP_TAGS:
                        form_links_dropped += 1
                    else:
                        form_links_kept += 1
                        if key_valid:
                            form_keys_from_formof.add(key)

            for fo in d.get("forms", []) or []:
                form_str = fo.get("form")
                if not form_str:
                    continue
                ftags = set(fo.get("tags", []) or [])
                if ftags & FORM_LINK_DROP_TAGS:
                    forms_arr_dropped += 1
                    continue
                forms_arr_kept += 1
                fkey = normalize(form_str)
                if is_valid_key(fkey):
                    form_keys_from_forms_arr.add(fkey)

            if line_no % SAMPLE_EVERY == 0:
                for fk, fv in d.items():
                    field_bytes[fk] += len(json.dumps(fv, ensure_ascii=False).encode("utf-8"))
                payload = build_line_payload(d)
                raw = dumps_compact(payload).encode("utf-8")
                gz = gzip.compress(raw, compresslevel=9)
                sample_gz_bytes += len(gz)
                sample_count += 1

    elapsed = time.time() - t0

    lemma_keys = set(key_to_word.keys())
    unique_form_keys = (form_keys_from_formof | form_keys_from_forms_arr) - lemma_keys

    avg_gz = sample_gz_bytes / sample_count if sample_count else 0
    est_lemma_total = avg_gz * len(lemma_keys)
    est_form_total = avg_gz * 1.05 * len(unique_form_keys)
    est_total = est_lemma_total + est_form_total

    try:
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            peak_rss *= 1024  # Linux reports KB; macOS reports bytes
    except Exception:
        peak_rss = None

    print()
    print("=" * 72)
    print("PHASE 1 REPORT")
    print("=" * 72)
    print(f"Runtime: {elapsed:.1f}s")
    if peak_rss:
        print(f"Peak RSS: {peak_rss / 1e6:.1f} MB")
    if malformed_lines:
        print(f"Malformed/unparsable lines skipped: {malformed_lines:,}")

    print()
    print("1. Entry counts")
    print(f"  total lines:               {total_lines:,}")
    print(f"  English (lang_code=en):    {english_lines:,}")
    print(f"  distinct words (English):  {len(words_seen):,}")
    print(f"  distinct (word,pos) pairs: {len(word_pos_seen):,}")

    print()
    print(f"2. Field size breakdown (every {SAMPLE_EVERY}th line across the WHOLE file, "
          f"{sample_count:,} English lines sampled)")
    total_field_bytes = sum(field_bytes.values()) or 1
    for fk, fv in field_bytes.most_common(25):
        print(f"  {fk:20s} {fv:>14,} bytes  ({100 * fv / total_field_bytes:5.1f}%)")

    print()
    print("3. Form resolution stats")
    print(f"  senses with form_of:                 {senses_with_form_of:,}")
    print(f"  senses with alt_of:                  {senses_with_alt_of:,}")
    print(f"  form/alt_of links kept (post-filter): {form_links_kept:,}")
    print(f"  form/alt_of links dropped:            {form_links_dropped:,}")
    print(f"  forms[] items kept:                   {forms_arr_kept:,}")
    print(f"  forms[] items dropped:                {forms_arr_dropped:,}")
    print(f"  unique keys from form_of/alt_of:       {len(form_keys_from_formof):,}")
    print(f"  unique keys from forms[]:              {len(form_keys_from_forms_arr):,}")
    print(f"  unique form keys (union, minus lemma keys): {len(unique_form_keys):,}")

    print()
    print("4. Top 30 sense tags")
    for tag, count in tag_counter.most_common(30):
        print(f"  {tag:25s} {count:>10,}")

    print()
    print("5. Estimated build output size -- APPROXIMATE, uncertain (see notes)")
    print(f"  sampled per-line payloads: {sample_count:,}, avg gzip size: {avg_gz:.0f} bytes/key")
    print(f"  unique lemma keys: {len(lemma_keys):,}  -> est. {est_lemma_total / 1e6:.1f} MB")
    print(f"  unique form keys:  {len(unique_form_keys):,}  -> est. {est_form_total / 1e6:.1f} MB")
    print(f"  ESTIMATED dict.bin size: {est_total / 1e6:.1f} MB")
    print("  NOTES:")
    print("  - Each (word,pos) LINE is gzipped standalone here; the real Phase 2 build")
    print("    merges same-word POS lines into one result object, so this may over- or")
    print("    under-count vs. the real build's shared envelope overhead / real dedup.")
    print("  - Every form key's payload is approximated as the average lemma payload size")
    print("    (+5% for the \"via\" note) since near-duplicate content compresses similarly.")
    print("    No claim is made that build-time dedup will shrink the total further --")
    print("    a 52 MB test slice showed 1,042 lemmas producing 3,091 keys, so form-key")
    print("    duplication is the dominant contributor to output size, not a marginal one.")

    print()
    print("6. Key-normalization stats")
    print(f"  unique normalized keys (lemmas only): {len(lemma_keys):,}")
    print(f"  case collisions (e.g. may/May):        {len(collision_keys):,}")
    pct_ipa = 100 * ipa_entries / english_lines if english_lines else 0
    pct_audio = 100 * audio_entries / english_lines if english_lines else 0
    print(f"  English entries with >=1 IPA:   {ipa_entries:,} ({pct_ipa:.1f}%)")
    print(f"  English entries with >=1 audio: {audio_entries:,} ({pct_audio:.1f}%)")


if __name__ == "__main__":
    main()
