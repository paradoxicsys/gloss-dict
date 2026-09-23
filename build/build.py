#!/usr/bin/env python3
"""Phase 2: build dict.idx + dict.bin from the kaikki.org JSONL extract.

One file read, two logical passes:

  Pass A (streaming): read every English line once. Trim each line's
    senses (cheap, drops the ~98% of bytes students don't need) and bucket
    them by (normalized key, original word, pos) -- so two lines that
    share a (word, pos) pair (e.g. two etymology sections both tagged
    "noun") get their senses merged into ONE entries[] item instead of
    two duplicate ones. Also record form_of/alt_of and forms[] links as
    source_key -> [(target_word_raw, via_note), ...].

  Pass B (in-memory, no re-read of the source file): for every key that
    has its own entries and/or incoming links, assemble the final
    "results" list (the key's own meanings first, then each linked lemma
    tagged with "via"), do the stable sort + MAX_SENSES cap per (word,pos)
    bucket, serialize, gzip, dedupe identical gzip blobs by content hash,
    append to dict.bin, and record (key, offset, length) for dict.idx.

Usage:
    python3 dict-api/build/build.py [path-to-jsonl] [--out-dir DIR]
"""
import argparse
import gzip
import hashlib
import json
import os
import resource
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.normalize import normalize, is_valid_key  # noqa: E402
from common.payload import (  # noqa: E402
    ATTRIBUTION,
    FORM_LINK_DROP_TAGS,
    MAX_SYNONYMS,
    clean_word_list,
    dumps_compact,
    finalize_senses,
    pick_audio,
    pick_ipa,
    trim_senses,
)
from common.idx_format import write_idx  # noqa: E402

DEFAULT_PATH = "/Users/shambhuyadav/Desktop/kaikki.org-dictionary-English.jsonl"
PROGRESS_EVERY = 200_000
GZIP_LEVEL = 9


def via_note_from_form_tags(tags, lemma_word):
    """Synthesize a readable via-note for a forms[]-array-derived key, e.g.
    tags=["plural"] -> "plural of cat". Auto-generated from raw tags, no
    curated phrase table -- good enough for v1, worth the architect's
    review (see the report's note)."""
    if tags:
        return f"{' '.join(tags)} of {lemma_word}"
    return f"form of {lemma_word}"


def dedup_preserve_order(items):
    seen = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?", default=DEFAULT_PATH)
    ap.add_argument(
        "--out-dir",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out"),
    )
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    t0 = time.time()
    build_version = int(time.time())

    total_lines = 0
    english_lines = 0
    duplicate_word_pos_lines = 0

    # key -> {original_word: {pos: bucket}}
    # bucket = {"sense_dicts": [...], "forms": [...], "syn_raw": [...], "ant_raw": [...]}
    entries_by_key = {}
    # key -> {original_word: (ipa, audio)}
    sounds_by_key = {}
    # source_key -> [(target_word_raw, via_note), ...]
    link_map = {}

    print(f"[build] Pass A: streaming {args.path} ...", file=sys.stderr)
    with open(args.path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            total_lines += 1
            if line_no % PROGRESS_EVERY == 0:
                elapsed = time.time() - t0
                print(f"[build] ... {line_no:,} lines ({elapsed:.0f}s elapsed)", file=sys.stderr)

            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("lang_code") != "en":
                continue
            english_lines += 1

            word = d.get("word", "")
            pos = d.get("pos", "")
            key = normalize(word)
            if not is_valid_key(key):
                continue

            word_buckets = entries_by_key.setdefault(key, {}).setdefault(word, {})
            is_new_pos = pos not in word_buckets
            bucket = word_buckets.setdefault(
                pos, {"sense_dicts": [], "forms": [], "syn_raw": [], "ant_raw": []}
            )
            if not is_new_pos:
                duplicate_word_pos_lines += 1

            bucket["sense_dicts"].extend(trim_senses(d.get("senses", []) or [], key))
            bucket["forms"].extend(
                fo.get("form") for fo in (d.get("forms", []) or []) if fo.get("form")
            )
            bucket["syn_raw"].extend(d.get("synonyms") or [])
            bucket["ant_raw"].extend(d.get("antonyms") or [])

            sounds = d.get("sounds", []) or []
            ipa = pick_ipa(sounds)
            audio = pick_audio(sounds)
            word_sounds = sounds_by_key.setdefault(key, {})
            cur_ipa, cur_audio = word_sounds.get(word, (None, None))
            word_sounds[word] = (cur_ipa or ipa, cur_audio or audio)

            # form_of / alt_of: THIS key -> target lemma, via = the
            # sense's own gloss (e.g. "simple past of go").
            for s in d.get("senses", []) or []:
                if not (s.get("form_of") or s.get("alt_of")):
                    continue
                stags = set(s.get("tags", []) or [])
                if stags & FORM_LINK_DROP_TAGS:
                    continue
                glosses = s.get("glosses") or []
                via_note = glosses[-1] if glosses else None
                if not via_note:
                    continue
                for t in (s.get("form_of") or []) + (s.get("alt_of") or []):
                    target_word = (t or {}).get("word")
                    if target_word:
                        # priority 0 = human-authored gloss text (preferred
                        # when the same target is reachable multiple ways)
                        link_map.setdefault(key, []).append((target_word, via_note, 0))

            # forms[] array: THIS word is a lemma; each surviving form
            # becomes a key linking back to it. kaikki often lists the SAME
            # spelled form under several old person/number tag combinations
            # (English "went" has no first/second/third-person distinction
            # today, but the source data still enumerates them) -- those
            # all point at the same (word) target and get collapsed in
            # Pass B, so the exact wording here only matters as a fallback.
            for fo in d.get("forms", []) or []:
                form_str = fo.get("form")
                if not form_str:
                    continue
                ftags = set(fo.get("tags", []) or [])
                if ftags & FORM_LINK_DROP_TAGS:
                    continue
                fkey = normalize(form_str)
                if not is_valid_key(fkey) or fkey == key:
                    continue
                via_note = via_note_from_form_tags(fo.get("tags", []), word)
                # priority 1 = auto-synthesized from raw tags (fallback)
                link_map.setdefault(fkey, []).append((word, via_note, 1))

    elapsed_a = time.time() - t0
    print(
        f"[build] Pass A done: {elapsed_a:.1f}s, {len(entries_by_key):,} lemma keys, "
        f"{len(link_map):,} keys with incoming links, "
        f"{duplicate_word_pos_lines:,} duplicate (word,pos) lines merged",
        file=sys.stderr,
    )

    # ---- Pass B: assemble, serialize, gzip, dedupe, write ----
    print("[build] Pass B: assembling + writing dict.bin / dict.idx ...", file=sys.stderr)

    idx_path = os.path.join(args.out_dir, "dict.idx")
    bin_path = os.path.join(args.out_dir, "dict.bin")

    all_keys = sorted(set(entries_by_key.keys()) | set(link_map.keys()))

    hash_to_pos = {}  # sha256 digest -> (offset, length), for dedup
    idx_entries = []  # (key, offset, length)
    lemma_key_count = 0
    form_only_key_count = 0
    dedup_hits = 0
    empty_key_count = 0
    dangling_links = 0

    with open(bin_path, "wb") as bin_f:
        offset = 0
        for i, key in enumerate(all_keys, start=1):
            if i % PROGRESS_EVERY == 0:
                elapsed = time.time() - t0
                print(
                    f"[build] ... {i:,}/{len(all_keys):,} keys assembled ({elapsed:.0f}s elapsed)",
                    file=sys.stderr,
                )

            results = []

            if key in entries_by_key:
                for original_word, word_buckets in entries_by_key[key].items():
                    entries = []
                    for pos, bucket in word_buckets.items():
                        senses, truncated = finalize_senses(bucket["sense_dicts"])
                        entry = {"pos": pos, "senses": senses}
                        forms = dedup_preserve_order(bucket["forms"])
                        if forms:
                            entry["forms"] = forms
                        ent_syn = clean_word_list(bucket["syn_raw"], key)[:MAX_SYNONYMS]
                        ent_ant = clean_word_list(bucket["ant_raw"], key)[:MAX_SYNONYMS]
                        if ent_syn:
                            entry["synonyms"] = ent_syn
                        if ent_ant:
                            entry["antonyms"] = ent_ant
                        entry["truncated"] = truncated
                        entries.append(entry)
                    ipa, audio = sounds_by_key.get(key, {}).get(original_word, (None, None))
                    result = {"word": original_word}
                    if ipa:
                        result["ipa"] = ipa
                    if audio:
                        result["audio"] = audio
                    result["entries"] = entries
                    results.append(result)

            if key in link_map:
                # Resolve every raw link to its (target_key, exact casing),
                # then collapse duplicates: kaikki commonly lists the same
                # target under several tag combinations (old person/number
                # slots that are spelled identically in modern English), so
                # without this a single real relationship (e.g. "went" is
                # simply the past tense of "go") would render as 5-7
                # near-identical blocks. Keep the best (lowest-priority,
                # i.e. gloss-sourced over tag-synthesized) via note per
                # target, first-seen order otherwise.
                best_for_target = {}  # (target_key, target_word) -> (via_note, priority, word_buckets)
                order = []
                for target_word_raw, via_note, priority in link_map[key]:
                    target_key = normalize(target_word_raw)
                    if target_key == key:
                        continue
                    target_bucket = entries_by_key.get(target_key)
                    if not target_bucket:
                        dangling_links += 1
                        continue
                    # Prefer the EXACT casing the link named (e.g. "go", not
                    # "GO" -- a different word that happens to share a
                    # normalized key). Only fall back to other casings under
                    # this key if that exact spelling isn't in the dataset.
                    if target_word_raw in target_bucket:
                        matches = [(target_word_raw, target_bucket[target_word_raw])]
                    else:
                        matches = list(target_bucket.items())
                    for target_original_word, word_buckets in matches:
                        dedup_id = (target_key, target_original_word)
                        if dedup_id not in best_for_target:
                            order.append(dedup_id)
                            best_for_target[dedup_id] = (via_note, priority, word_buckets)
                        elif priority < best_for_target[dedup_id][1]:
                            best_for_target[dedup_id] = (via_note, priority, word_buckets)

                for dedup_id in order:
                    target_key, target_original_word = dedup_id
                    via_note, _priority, word_buckets = best_for_target[dedup_id]
                    entries = []
                    for pos, bucket in word_buckets.items():
                        senses, truncated = finalize_senses(bucket["sense_dicts"])
                        entry = {"pos": pos, "senses": senses}
                        forms = dedup_preserve_order(bucket["forms"])
                        if forms:
                            entry["forms"] = forms
                        ent_syn = clean_word_list(bucket["syn_raw"], target_key)[:MAX_SYNONYMS]
                        ent_ant = clean_word_list(bucket["ant_raw"], target_key)[:MAX_SYNONYMS]
                        if ent_syn:
                            entry["synonyms"] = ent_syn
                        if ent_ant:
                            entry["antonyms"] = ent_ant
                        entry["truncated"] = truncated
                        entries.append(entry)
                    ipa, audio = sounds_by_key.get(target_key, {}).get(target_original_word, (None, None))
                    result = {"word": target_original_word, "via": via_note}
                    if ipa:
                        result["ipa"] = ipa
                    if audio:
                        result["audio"] = audio
                    result["entries"] = entries
                    results.append(result)

            # drop results that ended up with zero senses everywhere
            results = [r for r in results if any(e["senses"] for e in r["entries"])]
            if not results:
                empty_key_count += 1
                continue

            if key in entries_by_key:
                lemma_key_count += 1
            else:
                form_only_key_count += 1

            payload = {"query": key, "results": results, "attribution": ATTRIBUTION}
            raw = dumps_compact(payload).encode("utf-8")
            gz = gzip.compress(raw, compresslevel=GZIP_LEVEL)
            h = hashlib.sha256(gz).digest()

            if h in hash_to_pos:
                dedup_hits += 1
                o, length = hash_to_pos[h]
            else:
                o, length = offset, len(gz)
                bin_f.write(gz)
                offset += length
                hash_to_pos[h] = (o, length)

            idx_entries.append((key, o, length))

    write_idx(idx_path, build_version, idx_entries)

    elapsed = time.time() - t0
    try:
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            peak_rss *= 1024
    except Exception:
        peak_rss = None

    idx_size = os.path.getsize(idx_path)
    bin_size = os.path.getsize(bin_path)

    print()
    print("=" * 72)
    print("PHASE 2 BUILD REPORT")
    print("=" * 72)
    print(f"Runtime: {elapsed:.1f}s")
    if peak_rss:
        print(f"Peak RSS: {peak_rss / 1e6:.1f} MB")
    print(f"Build version (unix timestamp): {build_version}")
    print()
    print(f"Total lines: {total_lines:,}  English lines: {english_lines:,}")
    print(f"Duplicate (word,pos) lines merged (e.g. multi-etymology): {duplicate_word_pos_lines:,}")
    print()
    print(f"Total output keys: {len(idx_entries):,}")
    print(f"  lemma keys:     {lemma_key_count:,}")
    print(f"  form-only keys: {form_only_key_count:,}")
    print(f"Keys with no survivable content (dropped): {empty_key_count:,}")
    print(f"Dangling form/alt_of links (target not in dataset): {dangling_links:,}")
    print()
    print(f"dict.bin: {bin_size:,} bytes ({bin_size / 1e6:.1f} MB)")
    print(f"dict.idx: {idx_size:,} bytes ({idx_size / 1e6:.1f} MB)")
    if idx_entries:
        print(
            f"Dedup hits (byte-identical gzip blobs reused): {dedup_hits:,} "
            f"({100 * dedup_hits / len(idx_entries):.2f}% of keys)"
        )
    print(f"Unique stored blobs: {len(hash_to_pos):,}")


if __name__ == "__main__":
    main()
