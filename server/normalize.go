package main

// This is a deliberate line-for-line port of dict-api/common/normalize.py.
// The build (Python) and the server (Go) MUST compute identical keys for
// identical input, or a correctly-built key will never be found at request
// time. Any change here needs the matching change made there too.

import (
	"net/url"
	"strings"
	"unicode"

	"golang.org/x/text/unicode/norm"
)

const maxKeyLength = 64

var curlyQuotes = map[rune]rune{
	'‘': '\'', '’': '\'', // single quotes / apostrophes
	'“': '"', '”': '"', // double quotes
}

func isWordChar(r rune) bool {
	return unicode.IsLetter(r) || unicode.IsNumber(r)
}

func stripLeading(runes []rune) []rune {
	i := 0
	for i < len(runes) && !isWordChar(runes[i]) {
		i++
	}
	return runes[i:]
}

func stripTrailing(runes []rune) []rune {
	if len(runes) == 0 {
		return runes
	}
	end := len(runes)
	for end > 0 && !isWordChar(runes[end-1]) {
		end--
	}
	removed := runes[end:]
	core := runes[:end]
	if len(removed) == 1 && removed[0] == '.' && runesContain(core, '.') {
		// Abbreviation pattern (e.g. "U.S.") -- keep the trailing period.
		out := make([]rune, len(core)+1)
		copy(out, core)
		out[len(core)] = '.'
		return out
	}
	return core
}

func runesContain(runes []rune, target rune) bool {
	for _, r := range runes {
		if r == target {
			return true
		}
	}
	return false
}

// normalize mirrors common/normalize.py's normalize(): URL-decode, NFC,
// curly->straight quotes, strip edge punctuation (with the abbreviation
// exception), lowercase. Callers pass the RAW (still percent-encoded)
// segment, matching the Python side where normalize() does its own
// unquote() rather than relying on a framework to have decoded it already.
func normalize(raw string) string {
	s, err := url.PathUnescape(raw)
	if err != nil {
		s = raw // not validly escaped -- fall back to the raw string
	}
	s = norm.NFC.String(s)

	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		if repl, ok := curlyQuotes[r]; ok {
			b.WriteRune(repl)
		} else {
			b.WriteRune(r)
		}
	}
	s = b.String()

	runes := []rune(s)
	runes = stripLeading(runes)
	runes = stripTrailing(runes)

	return strings.ToLower(string(runes))
}

// isValidKey mirrors common/normalize.py's is_valid_key(): non-empty, at
// most 64 CODE POINTS (not bytes), and contains at least one letter.
func isValidKey(key string) bool {
	if key == "" {
		return false
	}
	runeCount := 0
	hasLetter := false
	for _, r := range key {
		runeCount++
		if unicode.IsLetter(r) {
			hasLetter = true
		}
	}
	if runeCount > maxKeyLength {
		return false
	}
	return hasLetter
}
