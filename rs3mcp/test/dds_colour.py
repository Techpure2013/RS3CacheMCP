"""A dds file made of one DXT1 block repeated, for feeding textures.dds() without the cache.

test_dds_rounding in correctness.py uses it.
"""

import struct

SIDE = 72  # the 32 pixel border comes off, so this leaves 8 by 8


def dds_file(block):
    head = bytearray(128)
    head[:4] = b"DDS "
    struct.pack_into("<II", head, 12, SIDE, SIDE)
    head[84:88] = b"DXT1"
    body = bytes(head) + block * ((SIDE // 4) * (SIDE // 4))
    return b"\x01" + struct.pack(">I", len(body)) + body
