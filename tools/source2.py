"""Decoders for the compiled Source 2 resources we need: textures (.vtex_c) and SVGs (.vsvg_c)."""
import struct

from PIL import Image

# VTexFormat values from the Source 2 engine.
FMT_DXT1 = 1
FMT_DXT5 = 2
FMT_RGBA8888 = 4
FMT_PNG_RGBA8888 = 16
FMT_PNG_DXT5 = 18
FMT_BC7 = 20
FMT_BGRA8888 = 28

EXTRA_COMPRESSED_MIP_SIZE = 4


def resource_blocks(data):
    """Return (header_size, {block_type: (offset, size)}) for a Source 2 resource file."""
    header_size, _header_version, _version, block_offset, block_count = struct.unpack_from("<IHHII", data, 0)
    blocks = {}
    for i in range(block_count):
        pos = 8 + block_offset + i * 12
        kind = data[pos : pos + 4].decode("latin1")
        rel, size = struct.unpack_from("<II", data, pos + 4)
        blocks[kind] = (pos + 4 + rel, size)
    return header_size, blocks


def lz4_block_decompress(src, expected_size):
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        literal = token >> 4
        if literal == 15:
            while True:
                b = src[i]
                i += 1
                literal += b
                if b != 255:
                    break
        dst += src[i : i + literal]
        i += literal
        if i >= n:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        match = token & 15
        if match == 15:
            while True:
                b = src[i]
                i += 1
                match += b
                if b != 255:
                    break
        match += 4
        start = len(dst) - offset
        if offset >= match:
            dst += dst[start : start + match]
        else:
            for k in range(match):
                dst.append(dst[start + k])
    if len(dst) != expected_size:
        raise ValueError(f"LZ4 size mismatch: got {len(dst)}, expected {expected_size}")
    return bytes(dst)


def _mip_bytes(fmt, w, h):
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    if fmt == FMT_DXT1:
        return bw * bh * 8
    if fmt in (FMT_DXT5, FMT_BC7):
        return bw * bh * 16
    if fmt in (FMT_BGRA8888, FMT_RGBA8888):
        return w * h * 4
    raise ValueError(f"unsupported texture format {fmt}")


def ycocg_to_rgb(img):
    """Undo the YCoCg packing some textures use (R=Co, G=Cg, B=scale, A=Y). Result is opaque."""
    import numpy as np

    px = np.asarray(img.convert("RGBA"), dtype=np.int32)
    co_raw, cg_raw, scale_raw, y = px[..., 0], px[..., 1], px[..., 2], px[..., 3]
    scale = (scale_raw >> 3) + 1
    co = np.trunc((co_raw - 128) / scale)
    cg = np.trunc((cg_raw - 128) / scale)
    rgb = np.stack([y + co - cg, y + cg, y - co - cg, np.full_like(y, 255)], axis=-1)
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGBA")


def decode_vtex(data):
    """Decode the largest mip of a compiled texture into a PIL RGBA image."""
    header_size, blocks = resource_blocks(data)
    img = _decode_vtex_pixels(data, header_size, blocks)
    # The texture compiler records YCoCg packing in the resource's edit info. It only
    # applies to the block-compressed data; textures that embed a PNG keep plain RGBA.
    fmt = data[blocks["DATA"][0] + 26]
    if fmt == FMT_DXT5 and "RED2" in blocks:
        start, size = blocks["RED2"]
        if b"YCoCg" in data[start : start + size]:
            img = ycocg_to_rgb(img)
    return img


def _decode_vtex_pixels(data, header_size, blocks):
    d, _ = blocks["DATA"]
    w, h, depth, fmt, mips = struct.unpack_from("<HHHBB", data, d + 20)
    extra_rel, extra_count = struct.unpack_from("<II", data, d + 32)
    pixels = data[header_size:]

    if fmt in (FMT_PNG_RGBA8888, FMT_PNG_DXT5):
        start = pixels.index(b"\x89PNG")
        end = pixels.index(b"IEND", start) + 8
        from io import BytesIO
        return Image.open(BytesIO(pixels[start:end])).convert("RGBA")

    compressed_sizes = None
    extra_base = d + 32
    for i in range(extra_count):
        pos = extra_base + extra_rel + i * 12
        kind, rel, _size = struct.unpack_from("<III", data, pos)
        if kind == EXTRA_COMPRESSED_MIP_SIZE:
            at = pos + 4 + rel
            _unk1, _unk2, count = struct.unpack_from("<III", data, at)
            compressed_sizes = list(struct.unpack_from(f"<{count}i", data, at + 12))

    raw_size = _mip_bytes(fmt, w, h) * max(1, depth)
    if compressed_sizes:
        # Mips are stored smallest first, so mip 0 is the final chunk.
        chunk = pixels[len(pixels) - compressed_sizes[0] :]
        mip0 = chunk if len(chunk) == raw_size else lz4_block_decompress(chunk, raw_size)
    else:
        mip0 = pixels[len(pixels) - raw_size :]

    if fmt == FMT_BGRA8888:
        return Image.frombuffer("RGBA", (w, h), mip0, "raw", "BGRA", 0, 1).copy()
    if fmt == FMT_RGBA8888:
        return Image.frombuffer("RGBA", (w, h), mip0, "raw", "RGBA", 0, 1).copy()
    pw, ph = ((w + 3) // 4) * 4, ((h + 3) // 4) * 4
    n = {FMT_DXT1: 1, FMT_DXT5: 3, FMT_BC7: 7}[fmt]
    img = Image.frombytes("RGBA", (pw, ph), mip0, "bcn", n)
    return img.crop((0, 0, w, h)) if (pw, ph) != (w, h) else img


def decode_vsvg(data):
    """Return the SVG source embedded in a compiled .vsvg_c file."""
    _header_size, blocks = resource_blocks(data)
    d, size = blocks["DATA"]
    body = data[d : d + size]
    return body[body.index(b"<svg") :].rstrip(b"\0").decode("utf-8")
