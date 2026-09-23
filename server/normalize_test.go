package main

import "testing"

func TestNormalize(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"lowercase passthrough", "cat", "cat"},
		{"uppercase folds", "Cats", "cats"},
		{"month name folds to verb key", "May", "may"},
		{"trailing comma stripped", "Running,", "running"},
		{"single trailing period stripped", "end.", "end"},
		{"abbreviation period kept", "U.S.", "u.s."},
		{"apostrophe kept (not an edge char)", "don't", "don't"},
		{"internal hyphen kept", "well-known", "well-known"},
		{"leading+trailing quotes stripped", `"quoted"`, "quoted"},
		{"curly single quotes to straight", "don’t", "don't"},
		{"curly double quotes to straight", "“quoted”", "quoted"},
		{"URL-encoded comma decodes then strips", "Running%2C", "running"},
		{"URL-encoded apostrophe decodes and is kept", "don%27t", "don't"},
		{"leading/trailing whitespace-ish punctuation", "  cat  ", "cat"},
		{"multiple edge punctuation stripped", "((cat))", "cat"},
		{"NFD input normalizes same as NFC", "café", "café"}, // e + combining acute -> café
		{"NFC input", "café", "café"},
		{"empty string", "", ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := normalize(c.in)
			if got != c.want {
				t.Errorf("normalize(%q) = %q, want %q", c.in, got, c.want)
			}
		})
	}
}

func TestNormalizeNFCParity(t *testing.T) {
	// The precomposed and decomposed forms of "café" must normalize to the
	// SAME key, or a build-time key (from Python's NFC-normalized source
	// text) could silently fail to match a request-time key typed with a
	// different Unicode representation of the same visible word.
	nfc := normalize("café")  // U+00E9 (precomposed é)
	nfd := normalize("café") // U+0065 U+0301 (e + combining acute)
	if nfc != nfd {
		t.Errorf("NFC/NFD forms diverged: %q vs %q", nfc, nfd)
	}
}

func TestIsValidKey(t *testing.T) {
	cases := []struct {
		name string
		key  string
		want bool
	}{
		{"normal word", "cat", true},
		{"empty", "", false},
		{"digits only, no letters", "123", false},
		{"single letter", "a", true},
		{"exactly 64 runes", repeatRune('a', 64), true},
		{"65 runes, over limit", repeatRune('a', 65), false},
		{"mixed letters and digits has a letter", "b52", true},
		{"punctuation only, no letters", "...", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := isValidKey(c.key)
			if got != c.want {
				t.Errorf("isValidKey(%q) = %v, want %v", c.key, got, c.want)
			}
		})
	}
}

func repeatRune(r rune, n int) string {
	out := make([]rune, n)
	for i := range out {
		out[i] = r
	}
	return string(out)
}
