"""RS3 sprites, index 8, as png files.

    python -m rs3mcp.sprites <id> [...] [-o folder]    write those sprites' pngs
    python -m rs3mcp.sprites verify [n]                decode every sprite, or the first n

The last two bytes of a sprite are its frame count, and the top bit of that picks the layout.
Clear is paletted: a header at the end gives the canvas, the palette size and each frame's offset
and size, with the palette just before it. Set is true colour: one header at the front, then each
frame's pixels and its alpha.

One png per sprite, the first frame with pixels on its full canvas, named sprites-<id>.png as the
readers expect. Pillow is not needed; the png is written by hand.
"""

import os
import struct
import sys
import zlib

from . import cachefile

MAJOR = 8


class Stop(Exception):
    pass


def _paletted(data, count):
    end = len(data) - 2
    head = end - 5 - count * 8
    if head < 0:
        raise Stop("a header for %d frames does not fit in %d bytes" % (count, len(data)))
    canvas_w, canvas_h, colours = struct.unpack_from(">HHB", data, head)
    colours += 1
    fields = struct.unpack_from(">%dH" % (count * 4), data, head + 5)
    xs, ys, ws, hs = (fields[i * count:(i + 1) * count] for i in range(4))
    pal_at = head - (colours - 1) * 3
    if pal_at < 0:
        raise Stop("a palette of %d colours does not fit" % colours)
    # one lookup table per channel, three translates
    pal = b"\0\0\0" + data[pal_at:head] + b"\0\0\0" * (256 - colours)
    red, green, blue = pal[0::3], pal[1::3], pal[2::3]
    solid = b"\0" + b"\xff" * 255

    frames, at = [], 0
    for f in range(count):
        w, h = ws[f], hs[f]
        n = w * h
        if at + 1 + n > pal_at:
            raise Stop("frame %d runs into the palette" % f)
        # an empty frame still has this byte
        flags = data[at]
        idx = data[at + 1:at + 1 + n]
        at += 1 + n
        alpha = None
        if flags & 2:
            if at + n > pal_at:
                raise Stop("frame %d's alpha runs into the palette" % f)
            alpha = data[at:at + n]
            at += n
        if flags & 1:
            # stored column by column, every h-th byte
            idx = b"".join(idx[y::h] for y in range(h))
            if alpha is not None:
                alpha = b"".join(alpha[y::h] for y in range(h))
        if n and max(idx) >= colours:
            raise Stop("frame %d uses colour %d of %d" % (f, max(idx), colours))
        px = bytearray(n * 4)
        px[0::4], px[1::4], px[2::4] = idx.translate(red), idx.translate(green), idx.translate(blue)
        # index 0 is see-through, unless alpha says otherwise
        px[3::4] = alpha if alpha is not None else idx.translate(solid)
        frames.append((xs[f], ys[f], w, h, bytes(px)))
    if at != pal_at:
        raise Stop("%d bytes between the last frame and the palette" % (pal_at - at))
    return canvas_w, canvas_h, frames


def _truecolour(data, count):
    if len(data) < 8:
        raise Stop("no header")
    kind, flags, w, h = struct.unpack_from(">BBHH", data, 0)
    if kind != 0:
        raise Stop("true colour type %d" % kind)
    frames, at, n = [], 6, w * h
    for f in range(count):
        rgb = data[at:at + n * 3]
        at += n * 3
        alpha = None
        if flags & 1:
            # right after the pixels, not two bytes late
            alpha = data[at:at + n]
            at += n
        if at > len(data) - 2:
            raise Stop("frame %d runs past the end" % f)
        px = bytearray(n * 4)
        px[0::4], px[1::4], px[2::4] = rgb[0::3], rgb[1::3], rgb[2::3]
        px[3::4] = alpha if alpha is not None else b"\xff" * n
        frames.append((0, 0, w, h, bytes(px)))
    if at != len(data) - 2:
        raise Stop("%d bytes left over" % (len(data) - 2 - at))
    return w, h, frames


def decode(data):
    """(canvas width, canvas height, [(x, y, w, h, rgba bytes)]) for one sprite group."""
    if len(data) < 2:
        raise Stop("too short")
    tail = struct.unpack_from(">H", data, len(data) - 2)[0]
    count = tail & 0x7FFF
    if not count:
        raise Stop("no frames")
    return (_truecolour if tail & 0x8000 else _paletted)(data, count)


def canvas(width, height, frame):
    """One frame placed on its canvas, as rgba rows. A sprite with an offset comes out at the
    size the game draws it, not the size of the frame alone."""
    x, y, w, h, px = frame
    if (x, y, w, h) == (0, 0, width, height):
        return px
    out = bytearray(width * height * 4)
    for row in range(min(h, height - y)):
        cols = min(w, width - x)
        src = row * w * 4
        dst = ((y + row) * width + x) * 4
        out[dst:dst + cols * 4] = px[src:src + cols * 4]
    return bytes(out)


def png(width, height, rgba):
    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))
    stride = width * 4
    raw = b"".join(b"\x00" + rgba[r * stride:(r + 1) * stride] for r in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _group_png(data):
    width, height, frames = decode(data)
    drawn = [f for f in frames if f[2] and f[3]]
    if not width or not height or not drawn:
        raise Stop("nothing to draw")
    return png(width, height, canvas(width, height, drawn[0]))


def _write_part(job):
    """Some groups into a folder: (written, {group: why it failed})."""
    folder, root, groups = job
    desc = cachefile.descriptor(MAJOR, root) or {}
    desc = {g: desc[g] for g in groups if g in desc}
    written, failed = 0, {}
    for g in groups:
        try:
            files = cachefile.files(MAJOR, g, root, desc) or {}
            if not files:
                raise Stop("no files")
            body = _group_png(files[min(files)])
        except (Stop, struct.error, IndexError, ValueError) as e:
            failed[g] = str(e)
            continue
        with open(os.path.join(folder, "sprites-%d.png" % g), "wb") as fh:
            fh.write(body)
        written += 1
    return written, failed


def write(folder, root=None, say=print, workers=None):
    """Every sprite into folder. Returns {group: why} for the ones that did not decode."""
    import multiprocessing
    root = root or cachefile.GAME
    groups = sorted(cachefile.descriptor(MAJOR, root) or {})
    os.makedirs(folder, exist_ok=True)
    workers = workers or min(32, os.cpu_count() or 1)
    step = max(1, len(groups) // (workers * 4))
    jobs = [(folder, root, groups[i:i + step]) for i in range(0, len(groups), step)]
    written, failed = 0, {}
    with multiprocessing.get_context("spawn").Pool(workers) as pool:
        for n, bad in pool.imap_unordered(_write_part, jobs):
            written += n
            failed.update(bad)
    say("sprites            %7d written%s" % (written, ", %d did not decode" % len(failed) if failed else ""))
    return failed


def main(argv):
    if argv[:1] == ["verify"]:
        desc = cachefile.descriptor(MAJOR) or {}
        groups = sorted(desc)
        if len(argv) > 1:
            groups = groups[:int(argv[1])]
        bad = {}
        for g in groups:
            try:
                files = cachefile.files(MAJOR, g, cachefile.GAME, desc) or {0: b""}
                decode(files[min(files)])
            except (Stop, struct.error, IndexError, ValueError) as e:
                bad[g] = str(e)
        print("%d sprites, %d did not decode" % (len(groups), len(bad)))
        for g in sorted(bad)[:20]:
            print("  %d: %s" % (g, bad[g]))
        return 1 if bad else 0
    out = "."
    if "-o" in argv:
        out = argv[argv.index("-o") + 1]
        argv = [a for a in argv if a not in ("-o", out)]
    for a in argv:
        g = int(a)
        files = cachefile.files(MAJOR, g) or {}
        if not files:
            print("  %d: no such sprite" % g)
            continue
        path = os.path.join(out, "sprites-%d.png" % g)
        with open(path, "wb") as fh:
            fh.write(_group_png(files[min(files)]))
        print("  " + path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
