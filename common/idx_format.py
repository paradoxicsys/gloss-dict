"""Binary format for dict.idx, shared by build.py (writer) and the Phase 3
server + spotcheck tools (reader).

Format (little-endian throughout):
  header: magic b"DIX1" (4 bytes), build_version (u64), key_count (u32)
  per key: key_len (u16), key (UTF-8, key_len bytes), offset (u64), length (u32)
"""
import struct

MAGIC = b"DIX1"
_HEADER_STRUCT = struct.Struct("<4sQI")
_KEY_LEN_STRUCT = struct.Struct("<H")
_OFFSET_LEN_STRUCT = struct.Struct("<QI")


def write_idx(path, build_version, entries):
    """entries: iterable of (key: str, offset: int, length: int), already
    in the order to be written (caller sorts if determinism is wanted)."""
    entries = list(entries)
    with open(path, "wb") as f:
        f.write(_HEADER_STRUCT.pack(MAGIC, build_version, len(entries)))
        for key, offset, length in entries:
            key_bytes = key.encode("utf-8")
            if len(key_bytes) > 0xFFFF:
                raise ValueError(f"key too long for u16 length prefix: {key!r}")
            f.write(_KEY_LEN_STRUCT.pack(len(key_bytes)))
            f.write(key_bytes)
            f.write(_OFFSET_LEN_STRUCT.pack(offset, length))


def read_idx(path):
    """Returns (build_version, {key: (offset, length)})."""
    out = {}
    with open(path, "rb") as f:
        header = f.read(_HEADER_STRUCT.size)
        magic, build_version, count = _HEADER_STRUCT.unpack(header)
        if magic != MAGIC:
            raise ValueError(f"bad magic: {magic!r}")
        for _ in range(count):
            (key_len,) = _KEY_LEN_STRUCT.unpack(f.read(_KEY_LEN_STRUCT.size))
            key = f.read(key_len).decode("utf-8")
            offset, length = _OFFSET_LEN_STRUCT.unpack(f.read(_OFFSET_LEN_STRUCT.size))
            out[key] = (offset, length)
    return build_version, out
