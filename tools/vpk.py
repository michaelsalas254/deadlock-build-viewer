"""Minimal reader for Valve VPK v2 archives (read-only)."""
import os
import struct

VPK_SIGNATURE = 0x55AA1234
EMBEDDED_ARCHIVE = 0x7FFF


class VPKEntry:
    __slots__ = ("path", "crc", "preload", "archive_index", "offset", "length")

    def __init__(self, path, crc, preload, archive_index, offset, length):
        self.path = path
        self.crc = crc
        self.preload = preload
        self.archive_index = archive_index
        self.offset = offset
        self.length = length

    @property
    def size(self):
        return len(self.preload) + self.length


class VPK:
    def __init__(self, dir_path):
        if not dir_path.endswith("_dir.vpk"):
            raise ValueError("expected a *_dir.vpk file")
        self.dir_path = dir_path
        self.prefix = dir_path[: -len("dir.vpk")]
        self.entries = {}
        with open(dir_path, "rb") as f:
            sig, version, tree_size = struct.unpack("<III", f.read(12))
            if sig != VPK_SIGNATURE:
                raise ValueError("not a VPK file")
            if version == 2:
                f.read(16)
                header_size = 28
            elif version == 1:
                header_size = 12
            else:
                raise ValueError(f"unsupported VPK version {version}")
            tree = f.read(tree_size)
        self.data_start = header_size + tree_size
        self._parse_tree(tree)

    def _parse_tree(self, tree):
        pos = 0

        def cstr():
            nonlocal pos
            end = tree.index(b"\0", pos)
            s = tree[pos:end].decode("utf-8", "replace")
            pos = end + 1
            return s

        while True:
            ext = cstr()
            if not ext:
                break
            while True:
                folder = cstr()
                if not folder:
                    break
                while True:
                    name = cstr()
                    if not name:
                        break
                    crc, preload_len, archive_index, offset, length, _term = struct.unpack_from("<IHHIIH", tree, pos)
                    pos += 18
                    preload = tree[pos : pos + preload_len]
                    pos += preload_len
                    folder_part = "" if folder == " " else folder + "/"
                    ext_part = "" if ext == " " else "." + ext
                    path = f"{folder_part}{name}{ext_part}"
                    self.entries[path] = VPKEntry(path, crc, preload, archive_index, offset, length)

    def __contains__(self, path):
        return path in self.entries

    def __iter__(self):
        return iter(self.entries)

    def read(self, path):
        e = self.entries[path]
        if e.length == 0:
            return e.preload
        if e.archive_index == EMBEDDED_ARCHIVE:
            archive, offset = self.dir_path, self.data_start + e.offset
        else:
            archive, offset = f"{self.prefix}{e.archive_index:03d}.vpk", e.offset
        with open(archive, "rb") as f:
            f.seek(offset)
            return e.preload + f.read(e.length)


DEFAULT_GAME_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\Deadlock\game\citadel"


def open_game_vpk(game_dir=DEFAULT_GAME_DIR):
    return VPK(os.path.join(game_dir, "pak01_dir.vpk"))
