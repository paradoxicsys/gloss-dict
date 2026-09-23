"""Student-facing payload trimming, shared by explore.py (Phase 1 estimate)
and build.py (Phase 2 real build) so both use IDENTICAL filtering logic.
"""
import json

from .normalize import normalize


def dumps_compact(payload):
    """The exact serialization stored in dict.bin: no unnecessary
    whitespace (gzip removes most of the redundancy anyway, but every byte
    still costs something before compression, and this keeps Phase 1's
    size estimate and Phase 2's real build using the same convention)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

ATTRIBUTION = "Wiktionary (via kaikki.org), CC BY-SA 4.0"

# Senses carrying any of these tags are hidden entirely from students.
HIDE_SENSE_TAGS = {"vulgar", "offensive", "derogatory", "ethnic", "slur"}

# Senses carrying any of these tags are stable-sorted to the end (kept, not hidden).
DEPRIORITIZE_SENSE_TAGS = {
    "obsolete", "archaic", "rare", "dialectal", "dated",
    "nonstandard", "slang", "historical",
}

# A form_of/alt_of link or a forms[] item carrying any of these tags is
# dropped as a lookup key (e.g. "book" as a dialectal past tense of "bake"
# must not shadow the real "book").
FORM_LINK_DROP_TAGS = {
    "obsolete", "archaic", "rare", "dialectal", "nonstandard",
    "misspelling", "dated", "pronunciation-spelling", "eye-dialect",
}

MAX_SENSES = 8
MAX_EXAMPLES = 2
MAX_SYNONYMS = 8
MAX_EXAMPLE_LEN = 200


def clean_word_list(items, headword_key):
    out = []
    for it in items or []:
        w = (it or {}).get("word", "")
        if not w:
            continue
        if "[" in w or "Thesaurus:" in w:
            continue
        if normalize(w) == headword_key:
            continue
        out.append(w)
    return out


def pick_ipa(sounds):
    first_any = None
    for s in sounds:
        ipa = s.get("ipa")
        if not ipa:
            continue
        if first_any is None:
            first_any = ipa
        if ipa.startswith("/") and ipa.endswith("/"):
            return ipa
    return first_any


def pick_audio(sounds):
    for s in sounds:
        if s.get("mp3_url"):
            return s["mp3_url"]
    return None


def trim_senses(senses_raw, headword_key):
    """Trim raw senses into lightweight sense dicts (each carrying a
    '_deprioritize' bool), WITHOUT sorting or capping yet. Split out from
    finalize_senses() so callers can accumulate trimmed senses from
    several source lines (e.g. two etymology sections sharing one part of
    speech) before the stable sort + MAX_SENSES cap is applied once, to
    the combined list."""
    kept = []
    for s in senses_raw:
        tags = s.get("tags", []) or []
        tagset = set(tags)
        if tagset & HIDE_SENSE_TAGS:
            continue
        glosses = s.get("glosses") or []
        if not glosses:
            continue
        definition = glosses[-1]

        raw_examples = s.get("examples", []) or []
        plain = [e for e in raw_examples if e.get("type") != "quotation"]
        quotes = [e for e in raw_examples if e.get("type") == "quotation"]
        examples = []
        for e in plain + quotes:
            text = e.get("text", "")
            if not text or len(text) > MAX_EXAMPLE_LEN:
                continue
            examples.append(text)
            if len(examples) >= MAX_EXAMPLES:
                break

        syns = clean_word_list(s.get("synonyms"), headword_key)[:MAX_SYNONYMS]

        entry = {"def": definition}
        if tags:
            entry["tags"] = tags
        if examples:
            entry["examples"] = examples
        if syns:
            entry["synonyms"] = syns
        entry["_deprioritize"] = bool(tagset & DEPRIORITIZE_SENSE_TAGS)
        kept.append(entry)
    return kept


def finalize_senses(sense_dicts):
    """Stable-sort deprioritized senses to the end, cap at MAX_SENSES,
    strip the internal '_deprioritize' marker. Returns (senses, truncated).

    Non-destructive (returns shallow copies) on purpose: a single lemma's
    sense_dicts list is reused every time a form key links to it (e.g.
    "go"'s senses get assembled again for "went", "going", "goes", "gone"),
    so mutating the shared dicts in place would break the second caller."""
    kept = sorted(sense_dicts, key=lambda x: x["_deprioritize"])  # stable
    truncated = len(kept) > MAX_SENSES
    kept = kept[:MAX_SENSES]
    out = []
    for k in kept:
        k2 = dict(k)
        del k2["_deprioritize"]
        out.append(k2)
    return out, truncated


def build_senses(senses_raw, headword_key):
    """Convenience: trim + finalize in one call, for a single source line."""
    return finalize_senses(trim_senses(senses_raw, headword_key))


def build_entry(d, key):
    """Build the trimmed per-(word,pos) 'entry' object (the item that goes
    into a result's "entries" list) from a raw kaikki line + its normalized
    key."""
    pos = d.get("pos", "")
    senses, truncated = build_senses(d.get("senses", []) or [], key)

    entry = {"pos": pos, "senses": senses}
    forms = [f.get("form") for f in (d.get("forms", []) or []) if f.get("form")]
    if forms:
        entry["forms"] = forms
    ent_syn = clean_word_list(d.get("synonyms"), key)[:MAX_SYNONYMS]
    ent_ant = clean_word_list(d.get("antonyms"), key)[:MAX_SYNONYMS]
    if ent_syn:
        entry["synonyms"] = ent_syn
    if ent_ant:
        entry["antonyms"] = ent_ant
    entry["truncated"] = truncated
    return entry


def build_line_payload(d):
    """Build a full response AS IF this single (word, pos) line were the
    sole content for its key (used by Phase 1's estimate, which does not
    merge multiple POS lines of the same word into one result)."""
    word = d.get("word", "")
    key = normalize(word)
    entry = build_entry(d, key)
    sounds = d.get("sounds", []) or []
    result = {"word": word, "entries": [entry]}
    ipa = pick_ipa(sounds)
    audio = pick_audio(sounds)
    if ipa:
        result["ipa"] = ipa
    if audio:
        result["audio"] = audio
    return {
        "query": word.lower(),
        "results": [result],
        "attribution": ATTRIBUTION,
    }
