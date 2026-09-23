package main

import (
	"os"
	"syscall"
)

// mmapFile memory-maps the whole file read-only. The returned []byte is
// backed by the mapping (not a copy); the caller must keep it alive for the
// process lifetime and call munmap (via Close) only at shutdown.
type mappedFile struct {
	Data []byte
	file *os.File
}

func mmapFile(path string) (*mappedFile, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	fi, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, err
	}
	size := fi.Size()
	if size == 0 {
		return &mappedFile{Data: []byte{}, file: f}, nil
	}
	data, err := syscall.Mmap(int(f.Fd()), 0, int(size), syscall.PROT_READ, syscall.MAP_SHARED)
	if err != nil {
		f.Close()
		return nil, err
	}
	return &mappedFile{Data: data, file: f}, nil
}

func (m *mappedFile) Close() error {
	var err error
	if len(m.Data) > 0 {
		err = syscall.Munmap(m.Data)
	}
	if cerr := m.file.Close(); err == nil {
		err = cerr
	}
	return err
}
