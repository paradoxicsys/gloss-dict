"""Shared word-key normalization for the dictionary API.

This is meant to be used IDENTICALLY by the offline build step (Phase 2)
and the online server (Phase 3), so a key computed at build time always
matches the key computed for an incoming request. Phase 1 (this repo's
exploration script) also uses it to compute normalized-key stats.

Rules (from the project spec):
1. URL-decode, then Unicode NFC.
2. Curly quotes -> straight quotes.
3. Strip punctuation from the EDGES only; keep internal characters
   ("U.S.", "don't", "well-known" are untouched internally).
   Special case: a single trailing period is stripped ("end." -> "end"),
   UNLESS the word still contains another period once that trailing
   period is removed, in which case it's an abbreviation and the period
   is kept ("U.S." stays "U.S.").
4. Lowercase.
5. A key is invalid (-> 400 at the server) if it's empty, longer than 64
   characters, or contains no letters at all.
"""
import re
import unicodedata
from urllib.parse import unquote

_CURLY_QUOTES = {
    "‘": "'", "’": "'",  # single quotes / apostrophes
    "“": '"', "”": '"',  # double quotes
}

MAX_KEY_LENGTH = 64


def _is_word_char(ch: str) -> bool:
    return ch.isalnum()


def _strip_leading(s: str) -> str:
    i = 0
    while i < len(s) and not _is_word_char(s[i]):
        i += 1
    return s[i:]


def _strip_trailing(s: str) -> str:
    if not s:
        return s
    end = len(s)
    while end > 0 and not _is_word_char(s[end - 1]):
        end -= 1
    removed = s[end:]
    core = s[:end]
    if removed == "." and "." in core:
        # Abbreviation pattern (e.g. "U.S.") -- keep the trailing period.
        return core + "."
    return core


def normalize(raw: str) -> str:
    """Normalize a headword (build time) or a clicked word (request time)
    into the lookup key used for the index."""
    s = unquote(raw)
    s = unicodedata.normalize("NFC", s)
    for curly, straight in _CURLY_QUOTES.items():
        s = s.replace(curly, straight)
    s = _strip_leading(s)
    s = _strip_trailing(s)
    return s.lower()


def is_valid_key(key: str) -> bool:
    if not key:
        return False
    if len(key) > MAX_KEY_LENGTH:
        return False
    if not any(ch.isalpha() for ch in key):
        return False
    return True
