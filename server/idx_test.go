package main

import (
	"encoding/binary"
	"os"
	"path/filepath"
	"testing"
)

// writeTestIdx hand-encodes a dict.idx file in the exact wire format
// (mirrors dict-api/common/idx_format.py's write_idx), independent of the
// production loadIndex() code, so this test actually checks the format
// contract rather than just round-tripping through the same function twice.
func writeTestIdx(t *testing.T, path string, buildVersion uint64, entries map[string]indexEntry) {
	t.Helper()
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()

	f.Write(magicBytes[:])
	binary.Write(f, binary.LittleEndian, buildVersion)
	binary.Write(f, binary.LittleEndian, uint32(len(entries)))
	for key, e := range entries {
		kb := []byte(key)
		binary.Write(f, binary.LittleEndian, uint16(len(kb)))
		f.Write(kb)
		binary.Write(f, binary.LittleEndian, e.Offset)
		binary.Write(f, binary.LittleEndian, e.Length)
	}
}

func TestLoadIndexRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "dict.idx")

	want := map[string]indexEntry{
		"cat":  {Offset: 0, Length: 100},
		"went": {Offset: 100, Length: 250},
		"café": {Offset: 350, Length: 12}, // non-ASCII key, exercises UTF-8 byte-length prefix
	}
	writeTestIdx(t, path, 1234567890, want)

	idx, err := loadIndex(path)
	if err != nil {
		t.Fatalf("loadIndex failed: %v", err)
	}
	if idx.BuildVersion != 1234567890 {
		t.Errorf("build version = %d, want 1234567890", idx.BuildVersion)
	}
	if len(idx.Entries) != len(want) {
		t.Fatalf("got %d entries, want %d", len(idx.Entries), len(want))
	}
	for k, wantEntry := range want {
		got, ok := idx.Entries[k]
		if !ok {
			t.Errorf("missing key %q", k)
			continue
		}
		if got != wantEntry {
			t.Errorf("entry[%q] = %+v, want %+v", k, got, wantEntry)
		}
	}
}

func TestLoadIndexBadMagic(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "dict.idx")
	os.WriteFile(path, []byte("XXXX\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"), 0644)

	_, err := loadIndex(path)
	if err == nil {
		t.Fatal("expected error for bad magic, got nil")
	}
}

func TestLoadIndexTruncated(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "dict.idx")
	// Valid header claiming 1 entry, but no entry bytes follow.
	f, _ := os.Create(path)
	f.Write(magicBytes[:])
	binary.Write(f, binary.LittleEndian, uint64(1))
	binary.Write(f, binary.LittleEndian, uint32(1))
	f.Close()

	_, err := loadIndex(path)
	if err == nil {
		t.Fatal("expected error for truncated index, got nil")
	}
}

func TestLoadIndexEmpty(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "dict.idx")
	writeTestIdx(t, path, 42, map[string]indexEntry{})

	idx, err := loadIndex(path)
	if err != nil {
		t.Fatalf("loadIndex failed on empty index: %v", err)
	}
	if len(idx.Entries) != 0 {
		t.Errorf("expected 0 entries, got %d", len(idx.Entries))
	}
}
