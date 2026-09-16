"""Sprite names, cracked from the name hashes in the sprite index.

The cache keeps a sprite's name only as a hash: Java's String.hashCode of the name as written.
Every string the database already holds is hashed, then every number in a name that matched is
swept, since names come in families (runepass_9_..., cinzel_13pt_bold). A match is exact, so a
cracked name is the real one. Most sprites never match anything.
"""

import re

STRING = re.compile(r'"([A-Za-z][A-Za-z0-9_ \-]{2,60})"')
DIGITS = re.compile(r"\d+")


def name_hash(s):
    h = 0
    for ch in s:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


def crack(hashes, words, seeds=()):
    """{sprite id: name} for (sprite id, hash) pairs, from candidate words and names already known."""
    want = {}
    for sid, h in hashes:
        want.setdefault(h, []).append(sid)
    found = {}

    def take(cand):
        got = want.get(name_hash(cand))
        for sid in got or ():
            found.setdefault(sid, cand)
        return bool(got)

    for w in words:
        for cand in {w, w.lower(), w.upper()}:
            take(cand)
    grown, tried = set(found.values()) | set(seeds), set()
    while grown:
        fresh = set()
        for name in grown:
            variants = {"%s_%d" % (name, k) for k in range(100)}
            for m in DIGITS.finditer(name):
                variants.update(name[:m.start()] + str(k) + name[m.end():] for k in range(400))
            for v in variants - tried:
                tried.add(v)
                if take(v):
                    fresh.add(v)
        grown = fresh - set(seeds)
    return found
