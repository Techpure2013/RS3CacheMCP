"""Clientscript opcode numbers after a game update, from the last update we read.

Jagex renumbers the opcodes every update but leaves most scripts alone. An unchanged script differs
from its old copy only in its opcode fields, so it pairs old numbers with new. The rest come from
changed scripts, lined up against their old copy. The old update lives in clientscript-reference.bin.

    python rs3mcp/cscalibrate.py check        does the map read the cache on disk
    python rs3mcp/cscalibrate.py run          calibrate the map against the reference and save it
    python rs3mcp/cscalibrate.py snapshot     make the cache on disk the reference
    python rs3mcp/cscalibrate.py simulate     scramble this cache as an update would and calibrate it back
"""

import collections
import difflib
import hashlib
import io
import json
import os
import struct
import sys
import time
import zlib

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rs3mcp import cachefile, common  # noqa: E402
from rs3mcp import clientscript as cs  # noqa: E402

REFERENCE = os.path.join(os.path.dirname(cs.OPMAP), "clientscript-reference.bin")
MAGIC = b"RSCS1"
KINDS = ("byte", "int", "const", "tribyte")
# id for an op the reference never had
NEW_OP = 900000
# readings kept per script, and unknowns allowed
TRIES = 64
UNKNOWNS = 6
# a script whose old copy pairs every unknown
WIDE_UNKNOWNS = 8
# share of scripts that must be unchanged
TRUST = 0.6


def cache_digest(root=None):
    """Which update this is, from the script index's group checksums."""
    desc = cachefile.descriptor(cs.MAJOR, root or cachefile.GAME) or {}
    h = hashlib.sha1()
    for g in sorted(desc):
        h.update(struct.pack(">II", g, desc[g]["crc"]))
    return h.hexdigest()


def _map_file(path=None):
    return json.load(io.open(path or cs.OPMAP, encoding="utf-8"))


def snapshot(path=REFERENCE, root=None):
    """The scripts on disk and the map that reads them, as the reference. Refuses a map that does not."""
    opmap = cs.load_map()
    blobs = dict(cs.scripts(root))
    for sid, raw in blobs.items():
        cs.read(raw, opmap)
    head = json.dumps({"digest": cache_digest(root), "made": time.strftime("%Y-%m-%d"),
                       "ops": {str(k): list(v) for k, v in sorted(opmap.items())}}).encode("utf-8")
    body = [MAGIC, struct.pack(">I", len(head)), head]
    for sid in sorted(blobs):
        body.append(struct.pack(">II", sid, len(blobs[sid])))
        body.append(blobs[sid])
    data = zlib.compress(b"".join(body), 9)
    if os.path.exists(path):
        # kept in data, so it does not ship
        os.makedirs(common.ROOT, exist_ok=True)
        os.replace(path, os.path.join(common.ROOT, "clientscript-reference.previous.bin"))
    with open(path, "wb") as fh:
        fh.write(data)
    return len(blobs)


def stale_reference(path=REFERENCE, root=None):
    """True when the reference is missing or holds an update other than the one on disk."""
    if not os.path.exists(path):
        return True
    return load_reference(path)[0]["digest"] != cache_digest(root)


def load_reference(path=REFERENCE):
    """(header, {scrambled: (op, kind)}, {script id: raw bytes})."""
    raw = zlib.decompress(open(path, "rb").read())
    if not raw.startswith(MAGIC):
        raise ValueError("%s is not a clientscript reference" % path)
    at = len(MAGIC)
    n = struct.unpack_from(">I", raw, at)[0]
    at += 4
    head = json.loads(raw[at:at + n].decode("utf-8"))
    at += n
    blobs = {}
    while at < len(raw):
        sid, size = struct.unpack_from(">II", raw, at)
        at += 8
        blobs[sid] = raw[at:at + size]
        at += size
    return head, {int(k): tuple(v) for k, v in head["ops"].items()}, blobs


def _opcode_fields(raw, opmap):
    """Where each instruction's opcode sits in the file, or None when the map cannot read it."""
    try:
        code, head = cs.footer(raw)
        starts = []
        cs.instructions(code, head["count"], opmap, starts)
    except (cs.Bad, struct.error, IndexError):
        return None
    return [s + 1 for s in starts]


def _numbered(raw, opmap):
    """[(number, immediate)] for a script the map reads, or None."""
    try:
        code, head = cs.footer(raw)
        starts = []
        got = cs.instructions(code, head["count"], opmap, starts)
    except (cs.Bad, struct.error, IndexError):
        return None
    return [((code[s] << 8) | code[s + 1], imm) for s, (_op, imm) in zip(starts, got)]


def from_unchanged(ref_map, ref, new):
    """{old number: Counter(new number)} from scripts differing only in opcode numbers. Numbers, not
    ops: the map gives some ops two numbers."""
    votes = collections.defaultdict(collections.Counter)
    unchanged = 0
    for sid, now in new.items():
        was = ref.get(sid)
        if was is None or len(was) != len(now):
            continue
        at = _opcode_fields(was, ref_map)
        if at is None:
            continue
        a, b = bytearray(was), bytearray(now)
        for p in at:
            a[p] = a[p + 1] = b[p] = b[p + 1] = 0
        if a != b:
            continue
        unchanged += 1
        for p in at:
            votes[(was[p] << 8) | was[p + 1]][(now[p] << 8) | now[p + 1]] += 1
    return votes, unchanged


def _settle(votes, taken=None):
    """{new number: old number}, one each way, the best voted first."""
    taken = dict(taken or {})
    claimed = {v: k for k, v in taken.items()}
    ranked = sorted(((n, old, num) for old, c in votes.items() for num, n in c.items()), reverse=True)
    for n, old, num in ranked:
        if old in claimed or num in taken:
            continue
        taken[num] = old
        claimed[old] = num
    return taken


def _readings(raw, known, kinds, limit=TRIES, unknowns=UNKNOWNS):
    """Ways to read a script with unknown numbers: [(instructions, {number: kind})]. Each unknown is
    tried at every width; a reading must use every instruction and byte."""
    try:
        code, head = cs.footer(raw)
    except (cs.Bad, struct.error, IndexError):
        return []
    count, n, found = head["count"], len(code), []
    # after a wrong width everything looks unknown
    budget = [limit * 256]

    def imm(kind, i):
        if kind == "byte":
            return (code[i], i + 1) if i + 1 <= n else None
        if kind == "int":
            return (struct.unpack_from(">i", code, i)[0], i + 4) if i + 4 <= n else None
        if kind == "tribyte":
            return ((code[i] << 16) | (code[i + 1] << 8) | code[i + 2], i + 3) if i + 3 <= n else None
        if i + 1 > n:
            return None
        sub = code[i]
        if sub == 0 and i + 5 <= n:
            return (("int", struct.unpack_from(">i", code, i + 1)[0]), i + 5)
        if sub == 1 and i + 9 <= n:
            return (("long", struct.unpack_from(">q", code, i + 1)[0]), i + 9)
        if sub == 2:
            stop = code.find(0, i + 1)
            return (("string", code[i + 1:stop].decode("latin-1")), stop + 1) if stop >= 0 else None
        return None

    def walk(i, out, guess):
        # branches only at a new unknown, not instructions
        mark = len(out)
        while len(found) < limit:
            if len(out) == count:
                if i == n:
                    found.append((list(out), dict(guess)))
                break
            if i + 2 > n:
                break
            num = (code[i] << 8) | code[i + 1]
            if num in known or num in guess or num in kinds:
                op, kind = known[num] if num in known else (("?", num), guess.get(num) or kinds[num])
                got = imm(kind, i + 2)
                if got is None:
                    break
                out.append((op, got[0]))
                i = got[1]
                continue
            if len(guess) >= unknowns:
                break
            for kind in KINDS:
                budget[0] -= 1
                if budget[0] < 0:
                    break
                got = imm(kind, i + 2)
                if got is None:
                    continue
                guess[num] = kind
                out.append((("?", num), got[0]))
                walk(got[1], out, guess)
                out.pop()
                del guess[num]
            break
        del out[mark:]

    walk(0, [], {})
    return found


def _aligned(theirs, mine):
    """Two instruction lists lined up, an unknown number standing as its own token."""
    a = [(op, str(imm)) for op, imm in theirs]
    b = [("?%d" % op[1] if isinstance(op, tuple) else op, str(imm)) for op, imm in mine]
    return a, b, difflib.SequenceMatcher(None, a, b, autojunk=False)


def _block_pairs(old_nums, a, b, sm, claimed, fits=lambda old, num: True):
    """(old number, new number) pairs where a reading differs from its old copy.

    Same-length blocks pair by position; others only one old against one unknown. Pairing in order,
    by immediate or by fitting width all saved wrong maps on simulated updates."""
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "replace":
            continue
        if i2 - i1 == j2 - j1:
            out += [(old, int(o[1:])) for old, (o, _) in zip(old_nums[i1:i2], b[j1:j2])
                    if isinstance(o, str) and old not in claimed]
        else:
            olds = {old for old in old_nums[i1:i2] if old not in claimed}
            unknowns = {int(o[1:]) for o, _ in b[j1:j2] if isinstance(o, str)}
            if len(olds) == 1 and len(unknowns) == 1:
                out.append((olds.pop(), unknowns.pop()))
    # an op keeps its width across updates
    return [(old, num) for old, num in out if fits(old, num)]


def from_changed(ref_map, ref, new, pairs):
    """More pairs, from scripts the map cannot read yet, repeated until nothing moves.

    The best reading against the old copy votes its pairs and widths. pairs is {new: old}. Returns
    (pairs, {new: width} for numbers nothing old claimed, widths)."""
    pairs, old_code, left = dict(pairs), {}, {}
    widths = collections.defaultdict(collections.Counter)
    for _ in range(8):
        known = _reader(ref_map, pairs, widths)
        claimed = set(pairs.values())
        votes = collections.defaultdict(collections.Counter)
        for sid, raw in new.items():
            if _opcode_fields(raw, known) is not None:
                continue
            found = _readings(raw, known, {})
            if not found:
                continue
            if sid in ref and sid not in old_code:
                nums = _numbered(ref[sid], ref_map)
                old_code[sid] = None if nums is None else (
                    [(ref_map[n][0], imm) for n, imm in nums], [n for n, _ in nums])
            theirs = old_code.get(sid)
            if theirs is None:
                if len(found) == 1:
                    for num, kind in found[0][1].items():
                        widths[num][kind] += 1
                continue
            old_ops, old_nums = theirs
            # best alignment, then most pairs at old width
            scored = []
            for code, guess in found:
                a, b, sm = _aligned(old_ops, code)
                got = set(_block_pairs(old_nums, a, b, sm, claimed,
                                       lambda old, num, g=guess: g.get(num) == ref_map[old][1]))
                scored.append(((sm.ratio(), len(got)), guess, got))
            top = max(s for s, _g, _p in scored)
            best = [(guess, got) for s, guess, got in scored if s == top]
            if len(best) == 1:
                for num, kind in best[0][0].items():
                    widths[num][kind] += 1
            # tied readings vote only what they agree on
            agreed = set.intersection(*[got for _g, got in best])
            for old, num in agreed:
                votes[old][num] += 1
        added = {n: old for n, old in _settle(votes, pairs).items() if n not in pairs}
        pairs.update(added)
        left = {n: c.most_common(1)[0][0] for n, c in widths.items() if n not in pairs}
        if not added:
            break
    return pairs, left, widths


def from_wide(ref_map, ref, new, known, pairs):
    """Pairs from scripts with more unknowns than the first pass tries, {new: old}.

    Taken only when the best reading against the old copy pairs every unknown one to one with an
    old op nothing claimed, at its old width. A script can hold several ops no other script uses."""
    claimed = {old for old in pairs.values() if old is not None}
    added = {}
    for sid, raw in sorted(new.items()):
        if sid not in ref or _opcode_fields(raw, known) is not None:
            continue
        nums = _numbered(ref[sid], ref_map)
        if nums is None:
            continue
        old_ops, old_nums = [(ref_map[n][0], imm) for n, imm in nums], [n for n, _ in nums]
        scored = []
        for code, guess in _readings(raw, known, {}, unknowns=WIDE_UNKNOWNS):
            a, b, sm = _aligned(old_ops, code)
            got = set(_block_pairs(old_nums, a, b, sm, claimed,
                                   lambda old, num, g=guess: g.get(num) == ref_map[old][1]))
            olds = {old for old, _ in got}
            if {num for _, num in got} == set(guess) and len(got) == len(guess) == len(olds):
                scored.append((sm.ratio(), got))
        scored.sort(key=lambda s: -s[0])
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            for old, num in scored[0][1]:
                added[num] = old
                known[num] = ref_map[old]
                claimed.add(old)
    pairs.update(added)
    return added


def _reader(ref_map, pairs, widths=None):
    """{new number: (op, width)}, preferring the width the new scripts read at."""
    out = {}
    for num, old in pairs.items():
        op, kind = ref_map[old]
        if widths and widths.get(num):
            kind = widths[num].most_common(1)[0][0]
        out[num] = (op, kind)
    return out


def calibrate(ref_ops, ref, new, say=print):
    """({new number: (op, width)}, {new number: old number or None}, report) for an update's scripts."""
    votes, unchanged = from_unchanged(ref_ops, ref, new)
    first = _settle(votes)
    say("calibration: %d of %d scripts unchanged, giving %d ops" % (unchanged, len(new), len(first)))

    pairs, left, widths = from_changed(ref_ops, ref, new, first)
    known = _reader(ref_ops, pairs, widths)
    # what matched nothing old is a new op
    fresh = {num: (NEW_OP + num, kind) for num, kind in sorted(left.items())}
    known.update(fresh)
    pairs.update({num: None for num in fresh})
    wide = from_wide(ref_ops, ref, new, known, pairs)
    if wide:
        say("calibration: %d more from scripts with many unknowns" % len(wide))
    unread = sum(1 for raw in new.values() if _opcode_fields(raw, known) is None)
    changed = len(pairs) - len(first) - len(fresh) - len(wide)
    say("calibration: %d more from changed scripts, %d new, %d scripts still unread"
        % (changed, len(fresh), unread))
    return known, pairs, {"unchanged": unchanged, "fromUnchanged": len(first), "fromChanged": changed,
                          "fromWide": len(wide), "new": len(fresh), "unread": unread}


def save_map(known, pairs, path=cs.OPMAP, keep=None):
    """The calibrated numbers written over the map; keep is where the old map goes.

    Each takes its old number's entry if that still names the op, else the op's entry."""
    old = _map_file(path)
    by_op = {v["op"]: v for v in old["ops"].values()}
    ops = {}
    for num, (op, kind) in sorted(known.items()):
        same = old["ops"].get(str(pairs.get(num)))
        entry = dict(same if same and same["op"] == op else by_op.get(op) or {"op": op, "in": None, "out": None})
        entry["imm"] = kind
        ops[str(num)] = entry
    if keep:
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        json.dump(old, io.open(keep, "w", encoding="utf-8"), indent=0, sort_keys=True)
    old["ops"] = ops
    old["calibrated"] = time.strftime("%Y-%m-%d")
    json.dump(old, io.open(path, "w", encoding="utf-8"), indent=0, sort_keys=True)


def _effects():
    """Every small stack effect an unknown op could have: (pops, pushes)."""
    from rs3mcp import csstack as st
    out = []
    for ni in range(6):
        for nl in range(3):
            for ns in range(3):
                for pi in range(3):
                    for pl in range(2):
                        for ps in range(2):
                            out.append(([st.I] * ni + [st.L] * nl + [st.S] * ns,
                                        [st.I] * pi + [st.L] * pl + [st.S] * ps))
    return out


def stack_ties(known, pairs, new, say=print):
    """Widths for scripts with more than one reading, from which one the stacks allow.

    Only readings with the fewest unknowns are tried, each unknown at every small effect. A reading
    no effect can simulate is wrong; one the checker cannot finish is kept. Readings that both
    simulate go to _likeliest. A script settles when one reading is left, and its unknowns go into
    known and pairs as new ops. Returns the number settled."""
    import shutil
    import tempfile
    from rs3mcp import csstack as st

    unread = {sid: raw for sid, raw in new.items() if _opcode_fields(raw, known) is None}
    choices = {}
    for sid, raw in unread.items():
        found = _readings(raw, known, {})
        if found:
            fewest = min(len(g) for _c, g in found)
            choices[sid] = [g for _c, g in found if len(g) == fewest]
    if not choices:
        return 0
    # each unknown at its first width
    trial = dict(known)
    for guesses in choices.values():
        for num, kind in guesses[0].items():
            trial.setdefault(num, (NEW_OP + num, kind))
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "opmap.json")
        shutil.copyfile(cs.OPMAP, path)
        save_map(trial, pairs, path=path)
        ctx = st.Context(path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    st.settle(ctx)
    effects = _effects()
    seen = collections.Counter()
    for sid, code in ctx.code.items():
        if sid not in choices:
            seen.update((a[0], b[0]) for a, b in zip(code, code[1:]))
    settled = 0
    for sid, readings in sorted(choices.items()):
        kept = []
        for guess in readings:
            opmap = dict(ctx.opmap)
            opmap.update({num: (NEW_OP + num, kind) for num, kind in guess.items()})
            head = cs.read(unread[sid], opmap)
            ctx.code[sid], ctx.heads[sid] = head.pop("code"), head
            code = list(ctx.code[sid])
            verdict = "wrong"
            for pops, pushes in effects if len(guess) == 1 else [None]:
                if pops is not None:
                    ctx.fixed[NEW_OP + next(iter(guess))] = (pops, pushes)
                try:
                    st.simulate(ctx, sid)
                    verdict = "clean"
                    break
                except st.Stuck:
                    verdict = "unsure"
                except st.Wrong:
                    pass
            for num in guess:
                ctx.fixed.pop(NEW_OP + num, None)
            if verdict != "wrong":
                kept.append((verdict, guess, code))
        clean = [(g, c) for v, g, c in kept if v == "clean"]
        left = clean or [(g, c) for _v, g, c in kept]
        how = "its stacks"
        if len(left) > 1:
            left, how = _likeliest(left, seen), "its stacks and the op pairs other scripts use"
        if len(left) == 1:
            for num, kind in left[0][0].items():
                known[num] = (NEW_OP + num, kind)
                pairs[num] = None
            settled += 1
            say("calibration: script %d settled by %s, %s" % (sid, how, left[0][0]))
    return settled


def _likeliest(readings, seen, margin=10):
    """[the one reading] whose op pairs the rest of the cache uses, else all of them.

    Each reading is scored by the rarest pair of neighbouring ops only it has, leaving out the
    unknown. It wins when that is margin times as common as the next reading's. A wrong width
    misframes the op after it into a pair the cache all but never uses."""
    def pairs(code):
        return {(a[0], b[0]) for a, b in zip(code, code[1:]) if a[0] < NEW_OP and b[0] < NEW_OP}
    own = [pairs(c) for _g, c in readings]
    shared = set.intersection(*own)
    scores = sorted(((min((seen[p] for p in mine - shared), default=float("inf")), k)
                     for k, mine in enumerate(own)), reverse=True)
    if scores[1][0] != float("inf") and scores[0][0] >= margin * max(1, scores[1][0]):
        return [readings[scores[0][1]]]
    return readings


def kept_numbers(known, current):
    """Fill known from the map in use when every op placed agrees with it, and say whether it did.

    The 2026-09-21 update kept its numbers: all 1,241 placed ops matched, and two scripts used ops
    no unchanged script did, so the update went unsaved over ops the map already had right. A real
    shuffle cannot agree that often by chance, hence the half-the-map floor."""
    if len(known) < len(current) // 2:
        return False
    if any(current.get(str(num), {}).get("op") != op for num, (op, _kind) in known.items()):
        return False
    for num, entry in current.items():
        known.setdefault(int(num), (entry["op"], entry.get("imm")))
    return True


def calibrate_cache(say=print, root=None):
    """Calibrate the cache on disk against the reference. Saves only when every script reads and
    enough are unchanged. Returns the report, or None without a reference."""
    if not os.path.exists(REFERENCE):
        say("calibration: no reference at %s" % REFERENCE)
        return None
    _head, ref_ops, ref = load_reference()
    new = dict(cs.scripts(root))
    known, pairs, report = calibrate(ref_ops, ref, new, say)
    if report["unread"] and kept_numbers(known, _map_file()["ops"]):
        report["unread"] = sum(1 for raw in new.values() if _opcode_fields(raw, known) is None)
        report["keptNumbers"] = True
        say("calibration: every op placed agrees with the map in use, so this update kept its "
            "numbers; %d scripts still unread" % report["unread"])
    if report["unread"] and root is None:
        report["settledByStacks"] = stack_ties(known, pairs, new, say)
        report["unread"] = sum(1 for raw in new.values() if _opcode_fields(raw, known) is None)
    share = report["unchanged"] / float(len(new) or 1)
    report["saved"] = report["unread"] == 0 and share >= TRUST
    if report["saved"]:
        save_map(known, pairs, keep=os.path.join(common.ROOT, "clientscript-opmap.previous.json"))
    elif report["unread"]:
        say("calibration: not saved, %d scripts still do not read" % report["unread"])
    else:
        say("calibration: not saved, only %.0f%% of scripts are unchanged since the reference, too "
            "few to trust the rest" % (share * 100))
    return report


def _scramble(ref_ops, ref, seed=7, changed=0.03, dropped=20):
    """A fake update: numbers moved, scripts edited or dropped, two new ops. Returns (scripts, truth, counts)."""
    import random
    rnd = random.Random(seed)
    old_nums = sorted(ref_ops)
    moved = dict(zip(old_nums, rnd.sample(range(0, 1600), len(old_nums))))
    spare = [n for n in range(0, 1600) if n not in set(moved.values())]
    brand = {"byte": spare[0], "int": spare[1]}
    truth = {moved[n]: ref_ops[n] for n in old_nums}
    truth[brand["byte"]] = (NEW_OP + brand["byte"], "byte")
    truth[brand["int"]] = (NEW_OP + brand["int"], "int")

    sids = sorted(ref)
    gone = set(rnd.sample(sids, dropped))
    edit = set(rnd.sample([s for s in sids if s not in gone], int(len(sids) * changed)))
    sites = rnd.sample(sorted(edit), 6)
    new_sites = {sid: ("int" if k % 3 == 0 else "byte") for k, sid in enumerate(sites)}
    out = {}
    for sid in sids:
        if sid in gone:
            continue
        raw = bytearray(ref[sid])
        at = _opcode_fields(bytes(raw), ref_ops)
        code_end = len(cs.footer(bytes(raw))[0]) + 1
        for p in at:
            num = moved[(raw[p] << 8) | raw[p + 1]]
            raw[p], raw[p + 1] = num >> 8, num & 255
        if sid in edit and at:
            count_at = code_end
            count = struct.unpack_from(">I", raw, count_at)[0]
            p = rnd.choice(at)
            kind = ref_ops[(ref[sid][p] << 8) | ref[sid][p + 1]][1]
            if sid in new_sites:
                which = new_sites[sid]
                num = brand[which]
                extra = bytes([num >> 8, num & 255]) + (b"\x05" if which == "byte" else b"\x00\x00\x01\x00")
                raw[p:p] = extra
                struct.pack_into(">I", raw, count_at + len(extra), count + 1)
            elif rnd.random() < 0.5 and kind == "byte":
                raw[p + 2] = (raw[p + 2] + 1) & 255
            else:
                # repeat the instruction
                size = (at[at.index(p) + 1] if at.index(p) + 1 < len(at) else code_end) - p
                raw[p:p] = raw[p:p + size]
                struct.pack_into(">I", raw, count_at + size, count + 1)
        out[sid] = bytes(raw)
    return out, truth, {"edited": len(edit), "dropped": len(gone)}


def simulate(seed=7, changed=0.03):
    """Scramble this cache, calibrate it back, and compare with the truth."""
    t = time.time()
    ref_ops = cs.load_map()
    ref = dict(cs.scripts())
    new, truth, did = _scramble(ref_ops, ref, seed, changed)
    print("simulated update: %d scripts edited, %d dropped, %d numbers" % (did["edited"], did["dropped"], len(truth)))
    known, _pairs, report = calibrate(ref_ops, ref, new)
    used = set()
    for raw in new.values():
        at = _opcode_fields(raw, truth)
        used.update((raw[p] << 8) | raw[p + 1] for p in at or [])
    wrong = {n: (known.get(n), truth[n]) for n in used if known.get(n) != truth[n]}
    print("numbers in use %d, calibrated right %d, wrong or missing %d, %.1fs"
          % (len(used), len(used) - len(wrong), len(wrong), time.time() - t))
    for n, (got, want) in sorted(wrong.items())[:12]:
        print("   %d: got %s, truth %s" % (n, got, want))
    return 1 if wrong or report["unread"] else 0


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    if cmd == ["check"]:
        fit = cs.verify()
        print("%d of %d scripts read with the map" % (fit["read"], fit["scripts"]))
        if os.path.exists(REFERENCE):
            head = load_reference()[0]
            print("reference made %s, %s this cache" % (head["made"], "is" if head["digest"] == cache_digest() else "is not"))
    elif cmd == ["run"]:
        print(json.dumps(calibrate_cache(), indent=1))
    elif cmd == ["snapshot"]:
        print("reference holds %d scripts" % snapshot())
    elif cmd == ["simulate"]:
        args = sys.argv[2:]
        sys.exit(simulate(int(args[0]) if args else 7, float(args[1]) if len(args) > 1 else 0.03))
    else:
        raise SystemExit(__doc__)
