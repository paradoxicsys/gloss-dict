package main

// Reader for dict.idx, matching dict-api/common/idx_format.py exactly:
//   header: magic "DIX1" (4 bytes), build_version (u64 LE), key_count (u32 LE)
//   per key: key_len (u16 LE), key (UTF-8, key_len bytes), offset (u64 LE), length (u32 LE)

import (
	"encoding/binary"
	"fmt"
	"os"
)

type indexEntry struct {
	Offset uint64
	Length uint32
}

type dictIndex struct {
	BuildVersion uint64
	Entries      map[string]indexEntry
}

var magicBytes = [4]byte{'D', 'I', 'X', '1'}

func loadIndex(path string) (*dictIndex, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if len(data) < 16 {
		return nil, fmt.Errorf("dict.idx too short: %d bytes", len(data))
	}
	var magic [4]byte
	copy(magic[:], data[0:4])
	if magic != magicBytes {
		return nil, fmt.Errorf("bad magic: %q", data[0:4])
	}
	buildVersion := binary.LittleEndian.Uint64(data[4:12])
	count := binary.LittleEndian.Uint32(data[12:16])

	entries := make(map[string]indexEntry, count)
	off := 16
	for i := uint32(0); i < count; i++ {
		if off+2 > len(data) {
			return nil, fmt.Errorf("truncated index at entry %d (key length)", i)
		}
		keyLen := int(binary.LittleEndian.Uint16(data[off : off+2]))
		off += 2
		if off+keyLen > len(data) {
			return nil, fmt.Errorf("truncated index at entry %d (key bytes)", i)
		}
		key := string(data[off : off+keyLen])
		off += keyLen
		if off+12 > len(data) {
			return nil, fmt.Errorf("truncated index at entry %d (offset/length)", i)
		}
		offset := binary.LittleEndian.Uint64(data[off : off+8])
		length := binary.LittleEndian.Uint32(data[off+8 : off+12])
		off += 12
		entries[key] = indexEntry{Offset: offset, Length: length}
	}
	if off != len(data) {
		return nil, fmt.Errorf("trailing garbage in dict.idx: %d unread bytes", len(data)-off)
	}

	return &dictIndex{BuildVersion: buildVersion, Entries: entries}, nil
}
