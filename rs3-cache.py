"""Query the extracted RS3 cache from a shell, as plain text.

The thing it exists for: a multi npc or loc is a parent config that wears a child's appearance
depending on a var. The entity in the world carries the parent's id, so anything naming the child
matches nothing. 'npc 1051' says which parent to name.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rs3mcp import common  # noqa: E402

DB = common.DB

# the short kind a person types -> config.type
KINDS = {
    "npc": "npcs",
    "loc": "objects",
    "obj": "items",
    "struct": "structs",
    "enum": "enums",
    "quest": "Quest",
    "seq": "sequences",
    "iface": "interfaces",
}

NONE16 = 65535


def die(msg):
    print("  " + msg)
    sys.exit(1)


def connect():
    if not os.path.exists(DB):
        die("no cache db at " + DB)
    # The extractor may still be filling it, so wait on a write lock rather than falling over.
    c = sqlite3.connect("file:" + DB.replace("\\", "/") + "?mode=ro", uri=True, timeout=15)
    c.execute("pragma busy_timeout = 15000")
    return c


def varname(c, varp, varbit):
    """The morph var, packed as varp in the low half and varbit in the high half."""
    if varbit != NONE16:
        # The morph record holds the raw varbit id; the table is keyed by the client's, which is
        # -(raw + 1). Off by one reads as a plausible but completely unrelated var, so it matters.
        r = c.execute("select name, base_name, base_id, mask, shift from varbit where id=?", (-(varbit + 1),)).fetchone()
        if r:
            return "varbit %s (bit %d of %s, mask %d)" % (r[0], r[4], r[1], r[3])
        return "varbit %d, not in the table" % varbit
    if varp != NONE16:
        r = c.execute("select name from config where type='VarPlayer' and id=?", (varp,)).fetchone()
        return "varp %s (%d)" % (r[0] if r else "?", varp)
    return "no var"


def children(m):
    """The value-indexed list, and the id used when the var reads past the end of it."""
    return list(m.get("ids") or []), m.get("fallback")


def morphsOf(data):
    d = json.loads(data) if data else {}
    for key in ("morphs", "morphsWide"):
        m = d.get(key)
        if isinstance(m, dict):
            yield key, m


def runs(pairs):
    """Collapse index -> child into contiguous ranges, since morph tables repeat for dozens of values."""
    out = []
    for i, ch in pairs:
        if out and out[-1][2] == ch and out[-1][1] == i - 1:
            out[-1][1] = i
        else:
            out.append([i, i, ch])
    return out


def searchIn(c, kind, word, limit=40):
    """Config name first, then display name. The display name is inside the json blob, so LIKE
    narrows the rows down and the parse is what actually confirms the match."""
    t = KINDS[kind]
    like = "%" + word + "%"
    out, seen = [], set()

    for i, n in c.execute("select id,name from config where type=? and name like ? order by name limit ?", (t, like, limit)):
        seen.add(i)
        out.append((kind, i, n, None))

    if len(out) < limit:
        for i, n, data in c.execute("select id,name,data from config where type=? and data like ? limit 600", (t, like)):
            if i in seen:
                continue
            d = json.loads(data) if data else {}
            dn = d.get("name")
            if dn and word.lower() in dn.lower():
                out.append((kind, i, n, dn))
                if len(out) >= limit:
                    break
    return out


def searchEverywhere(c, word, limit=30):
    out = []
    for kind in KINDS:
        out.extend(searchIn(c, kind, word, limit - len(out)))
        if len(out) >= limit:
            break
    return out


def row(c, kind, cid):
    return c.execute("select id,name,api,data from config where type=? and id=?", (KINDS[kind], cid)).fetchone()


def listHits(hits):
    for k, i, n, dn in hits[:14]:
        print("    %-5s %-8d %-46s %s" % (k, i, n, dn or ""))


def resolve(c, kind, word):
    """An id, a config name, or a display name. A miss looks in the other kinds before giving up,
    since half the time the thing asked for as an obj is a loc."""
    if word.lstrip("-").isdigit():
        r = row(c, kind, int(word))
        if r:
            return r
        die("no %s with id %s" % (kind, word))

    r = c.execute("select id,name,api,data from config where type=? and name=? collate nocase", (KINDS[kind], word)).fetchone()
    if r:
        return r

    hits = searchIn(c, kind, word)
    if not hits:
        other = searchEverywhere(c, word)
        if not other:
            die("nothing called " + word)
        print("  no %s called %s, but this is:" % (kind, word))
        listHits(other)
        sys.exit(0)

    if len(hits) > 1:
        print("  %d matches, first one shown:" % len(hits))
        listHits(hits)
        print("")
    return row(c, kind, hits[0][1])


def parentsOf(c, kind, cid):
    """Who wears this config as one of their appearances. LIKE prefilters, the parse confirms."""
    t = KINDS[kind]
    out = []
    rows = c.execute("select id,name,data from config where type=? and data like ?", (t, "%" + str(cid) + "%"))
    for pid, pname, data in rows:
        if pid == cid:
            continue
        for key, m in morphsOf(data):
            lst, fallback = children(m)
            if cid in lst or cid == fallback:
                out.append((pid, pname))
                break
    return out


def paramsOf(c, kind, cid):
    return c.execute(
        "select p.param_id, t.name, t.valuetype, p.int_value, p.str_value, p.target_name "
        "from param_use p left join param_type t on t.param_id = p.param_id "
        "where p.owner_type=? and p.owner_id=? order by p.param_id", (KINDS[kind], cid)).fetchall()


def showParams(c, kind, cid):
    """The params are where the useful things hide: an ability's icon and description, an item's
    requirements and bonuses. A SPRITE param is worth calling out, since it is previewable."""
    rows = paramsOf(c, kind, cid)
    if not rows:
        return
    print("    params")
    for pid, pname, vtype, ival, sval, target in rows:
        v = sval if sval is not None else ival
        if target:
            v = "%s  -> %s" % (v, target)
        elif vtype == "SPRITE" and ival is not None:
            v = "%s  (cache sheet %d to look at it)" % (ival, ival)
        print("      %-6d %-44s %s" % (pid, pname or "", str(v)[:90]))


def showFields(c, kind, cid, d):
    """Everything the cache carries that is not already printed above. Long values are clipped
    rather than dropped, because a truncated model list still tells you it is there."""
    skip = {"name", "morphs", "morphsWide"}
    plain = {k: v for k, v in d.items()
             if k not in skip and not re.match(r"(option|stack)\d$", k)}
    if not plain:
        return
    print("    fields")
    for k in sorted(plain):
        v = plain[k]
        if isinstance(v, (dict, list)):
            v = json.dumps(v, separators=(",", ":"))
        print("      %-28s %s" % (k, str(v)[:88]))


def showStacks(c, kind, d):
    """Items past id 65535 moved to the tribyte stack table, so this is the only one that fills in."""
    keys = sorted(k for k in d if re.match(r"stack\d$", k))
    if not keys:
        return
    print("    stack sizes")
    for k in keys:
        tid, qty = d[k]["item"], d[k]["count"]
        r = c.execute("select data from config where type='items' and id=?", (tid,)).fetchone()
        dn = (json.loads(r[0]).get("name") if r and r[0] else None) or "no name"
        print("      %8d -> id %-7d %s" % (qty, tid, dn))


def showConfig(c, kind, word, verbose=False):
    cid, name, api, data = resolve(c, kind, word)
    d = json.loads(data) if data else {}
    print("  %s %d  %s" % (kind, cid, name))
    if api:
        print("    api          %s" % api)
    if d.get("name"):
        print("    display      %s" % d["name"])

    for i in range(1, 6):
        a = d.get("option%d" % i)
        if a:
            print("    action %d     %s" % (i, a))

    if kind == "enum":
        rows = c.execute("select key,value from enum_entry where enum_id=?", (cid,)).fetchall()
        if rows:
            print("    %d entries" % len(rows))
            for k, v in rows[:60]:
                print("      %-12s %s" % (k, v))
            if len(rows) > 60:
                print("      ... %d more" % (len(rows) - 60))

    if kind == "seq":
        showSeq(c, cid, d)

    anim = d.get("animation")
    if anim is not None:
        r = c.execute("select name from config where type='sequences' and id=?", (anim,)).fetchone()
        print("    animation    %d  %s" % (anim, r[0] if r else ""))

    quests = d.get("quests") if kind == "loc" else None
    if quests:
        names = []
        for q in (quests if isinstance(quests, list) else [quests]):
            r = c.execute("select name from config where type='Quest' and id=?", (q,)).fetchone()
            names.append(r[0] if r else str(q))
        print("    requires     %s" % ", ".join(names))

    # a variant item stores only who it is based on and which template draws it
    for link, tmpl, label in (("noteLink", "noteTemplate", "noted"), ("lentLink", "lentTemplate", "lent"),
                              ("boughtLink", "boughtTemplate", "bought"), ("shardLink", "shardTemplate", "shard")):
        if link in d:
            r = c.execute("select name,data from config where type='items' and id=?", (d[link],)).fetchone()
            dn = (json.loads(r[1]).get("name") if r and r[1] else None) if r else None
            side = "this IS the %s variant of" % label if tmpl in d else "its %s variant is" % label
            print("    %-12s %s %s (%d)" % (label, side, (dn or (r[0] if r else "?")), d[link]))

    if kind == "obj":
        sold = c.execute("select s.inv_id, i.name, s.quantity from inv_stock s "
                         "join inv i on i.inv_id = s.inv_id where s.item_id=? order by s.inv_id", (cid,)).fetchall()
        for inv_id, iname, qty in sold[:12]:
            print("    sold in      %-5d %-34s x%d" % (inv_id, iname or "", qty))
        if len(sold) > 12:
            print("                 ... and %d more shops" % (len(sold) - 12))

    showStacks(c, kind, d)
    showParams(c, kind, cid)
    if verbose:
        showFields(c, kind, cid, d)

    # only locs and npcs wear another config's appearance, so the question is meaningless elsewhere
    if kind in ("loc", "npc"):
        ps = parentsOf(c, kind, cid)
        if ps:
            print("    child of     " + ", ".join("%s (%d)" % (p[1], p[0]) for p in ps))
            print("                 the world carries the parent's id, not this one")
        else:
            print("    child of     nothing, this id stands on its own")

    for key, m in morphsOf(data):
        u1 = m.get("var", 0)
        print("    morphs on    %s" % varname(c, u1 & 0xFFFF, (u1 >> 16) & 0xFFFF))
        lst, fallback = children(m)

        def label(ch):
            r = c.execute("select id,name,data from config where type=? and id=?", (KINDS[kind], ch)).fetchone()
            if not r:
                return "%d, no config of this kind" % ch
            d2 = json.loads(r[2]) if r[2] else {}
            dn = d2.get("name")
            return "%s (%d)%s" % (r[1], ch, "  " + dn if dn else "")

        for lo, hi, ch in runs([(i, v) for i, v in enumerate(lst) if v != NONE16]):
            span = str(lo) if lo == hi else "%d-%d" % (lo, hi)
            print("      %-10s %s" % (span, label(ch)))
        if isinstance(fallback, int) and fallback != NONE16:
            print("      %-10s %s" % ("otherwise", label(fallback)))


def showSeq(c, cid, d):
    """A sequence is a list of frames, each naming an archive in cache index 48 and how long to
    hold it. A duration is in 20ms animation cycles, not game ticks."""
    frames = d.get("frames") or {}
    if frames.get("durations"):
        ticks = sum(frames["durations"])
        arch = sorted(set(frames["groups"]))
        print("    frames       %d, %d cycles (%d ms)" % (len(frames["durations"]), ticks, ticks * 20))
        print("    frame data   archive %s of cache index 48" % ", ".join(str(a) for a in arch))
    hits = []
    for lid, lname, data in c.execute("select id,name,data from config where type='objects' and data like ?",
                                      ("%" + '"animation":' + str(cid) + "%",)):
        if json.loads(data).get("animation") == cid:
            hits.append((lid, lname))
    for lid, lname in hits[:12]:
        print("    played by    %-7d %s" % (lid, lname or ""))


def itemName(c, iid):
    r = c.execute("select name,data from config where type='items' and id=?", (iid,)).fetchone()
    if not r:
        return ("", "")
    return (r[0] or "", (json.loads(r[1]).get("name") if r[1] else "") or "")


def showShop(c, word):
    """Shop stock lives in invtype, page 5 of the config index.

    Note what the cache does NOT say: nothing links an npc to its shop. There is no param typed
    INV on any npc, so the connection is made in clientscript. ARMOURSHOP is Horvik's by name and
    convention, not because the cache says so."""
    q = "select inv_id, name, size, entries from inv where "
    if word.lstrip("-").isdigit():
        rows = c.execute(q + "inv_id=?", (int(word),)).fetchall()
    else:
        rows = c.execute(q + "name like ? collate nocase order by inv_id", ("%" + word + "%",)).fetchall()
    if not rows:
        die("no shop or container matching " + word)

    if len(rows) > 1:
        # an exact name wins outright, otherwise show the list and only expand a lone stocked hit
        exact = [r for r in rows if (r[1] or "").lower() == word.lower()]
        if len(exact) == 1:
            rows = exact
        else:
            print("  %d containers match, stocked first:" % len(rows))
            for iid, nm, size, ent in sorted(rows, key=lambda r: (r[3] == 0, r[0]))[:25]:
                print("    %-5d %-42s size %-5s %s" % (iid, nm or "", size,
                      "%d lines" % ent if ent else "no stock"))
            stocked = [r for r in rows if r[3]]
            if len(stocked) != 1:
                return
            rows = stocked
            print("")

    iid, nm, size, ent = rows[0]
    print("  inv %d  %s" % (iid, nm or ""))
    print("    size         %s" % size)
    if not ent:
        print("    no stock, so it is a container rather than a shop")
        return
    print("    stock")
    for slot, item, qty in c.execute("select slot,item_id,quantity from inv_stock where inv_id=? order by slot", (iid,)):
        cn, dn = itemName(c, item)
        print("      %-3d %-7d x%-6d %-32s %s" % (slot, item, qty, dn, cn))


def showParam(c, word):
    if word.lstrip("-").isdigit():
        r = c.execute("select * from param_type where param_id=?", (int(word),)).fetchone()
    else:
        r = c.execute("select * from param_type where name like ? collate nocase order by length(name) limit 1",
                      ("%" + word + "%",)).fetchone()
    if not r:
        die("no param matching " + word)
    pid, pname, vtype, valuetype, target = r[0], r[1], r[2], r[3], r[4]
    print("  param %d  %s" % (pid, pname or ""))
    print("    type         %s" % (valuetype or vtype))
    if target:
        print("    points at    %s" % target)
    for otype, n in c.execute("select owner_type, count(*) from param_use where param_id=? "
                              "group by owner_type order by 2 desc", (pid,)):
        print("    used by      %-14s %d" % (otype, n))
    for otype, oid, iv, sv, tn in c.execute(
            "select owner_type,owner_id,int_value,str_value,target_name from param_use where param_id=? limit 8", (pid,)):
        nm = c.execute("select name from config where type=? and id=?", (otype, oid)).fetchone()
        v = sv if sv is not None else iv
        print("      %-10s %-8d %-38s %s%s" % (otype, oid, (nm[0] if nm else "")[:38], v,
                                               "  -> " + tn if tn else ""))


def varLines(c, word, limit=40):
    """Varbits and the base vars they are packed into, both of which answer to a name or an id."""
    out = []
    if word.lstrip("-").isdigit():
        i = int(word)
        bits = c.execute("select id,name,base_name,base_id,mask,shift from varbit where id in (?,?)", (i, -i)).fetchall()
    else:
        bits = c.execute(
            "select id,name,base_name,base_id,mask,shift from varbit where name like ? order by name limit ?",
            ("%" + word + "%", limit),
        ).fetchall()
    for r in bits:
        out.append("  varbit %-52s bit %-2d of %s (%d), mask %d" % (r[1], r[5], r[2], r[3], r[4]))

    # config.VarPlayer carries the varbits too, under the same negative ids, so keep to the real ones
    base = c.execute(
        "select id,name,type from config where type in ('VarPlayer','VarClient') and id >= 0"
        " and (name like ? or cast(id as text)=?) order by name limit ?",
        ("%" + word + "%", word, limit),
    ).fetchall()
    for b in base:
        n = c.execute("select count(*) from varbit where base_id=?", (b[0],)).fetchone()[0]
        packed = ", %d varbits packed into it" % n if n else ""
        out.append("  %-6s %-52s id %d%s" % (b[2], b[1], b[0], packed))
    return out


def showVar(c, word):
    lines = varLines(c, word)
    if not lines:
        print("  nothing called " + word)
    for line in lines:
        print(line)


def showFind(c, kind, word):
    hits = searchIn(c, kind, word) if kind else searchEverywhere(c, word, 40)
    for k, i, n, dn in hits:
        print("  %-5s %-8d %-46s %s" % (k, i, n, dn or ""))

    # Only worth sweeping the vars when nothing was pinned to a kind, otherwise it is noise
    lines = varLines(c, word, 12) if not kind else []
    for line in lines:
        print(line)

    if not hits and not lines:
        print("  nothing matching " + word)


SPRITES = common.SPRITES


def spriteLabel(c, sid):
    """Sprites carry a name hash rather than a name, so most of them only answer to an id. Best
    available, in order: the cracked real name, then whatever config points at it through a param
    typed SPRITE (that is where the buff and ability icons live), then the interface that draws it."""
    r = c.execute("select name from sprite_name where sprite_id=?", (sid,)).fetchone()
    if r and r[0]:
        return r[0]
    r = c.execute("select owner_name from sprite_use where sprite_id=? and owner_name is not null"
                  " order by (source='iface'), length(owner_name) limit 1", (sid,)).fetchone()
    return r[0] if r else ""


def spriteSpec(c, spec, limit=400):
    """What to put on the sheet. Three forms, because the useful cases are different shapes:

      A=19558,B=19559     explicit labels, for an alphabet or an ordered set
      19558-19576,19580   ids and ranges, for a block that is contiguous apart from gaps
      skillcape           anything whose recovered name or drawing component matches

    Returns [(label, id)] in the order given, since sheet order is usually meaningful."""
    out = []
    if "=" in spec:
        for part in spec.split(","):
            if not part.strip():
                continue
            label, _, num = part.partition("=")
            if not num.strip().isdigit():
                die("'%s' is not label=id" % part.strip())
            out.append((label.strip(), int(num)))
        return out

    if re.fullmatch(r"[\d,\-\s]+", spec):
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo, _, hi = part.partition("-")
                if not (lo.strip().isdigit() and hi.strip().isdigit()):
                    die("'%s' is not a range" % part)
                for i in range(int(lo), int(hi) + 1):
                    out.append((str(i), i))
            elif part.isdigit():
                out.append((part, int(part)))
        return out

    like = "%" + spec + "%"
    seen = set()
    for q in ("select sprite_id, name from sprite_name where name like ? collate nocase order by sprite_id",
              "select sprite_id, min(owner_name) from sprite_use where owner_name like ? collate nocase"
              " group by sprite_id order by sprite_id"):
        for sid, nm in c.execute(q, (like,)):
            if sid in seen:
                continue
            seen.add(sid)
            out.append((nm or str(sid), sid))
            if len(out) >= limit:
                return out
    return out


def grabSprite(c, argv):
    """Just the png, no sheet around it. Copies out of the extracted set named by whatever the
    sprite answers to, so you get necrosis.png rather than sprites-30101.png."""
    out = os.path.dirname(os.path.abspath(__file__))
    show = False
    spec = []
    i = 0
    while i < len(argv):
        if argv[i] in ("-o", "--out") and i + 1 < len(argv):
            out = argv[i + 1]; i += 2
        elif argv[i] in ("-show", "--show"):
            show = True; i += 1
        else:
            spec.append(argv[i]); i += 1
    if not spec:
        die("grab wants an id or some text")
    picks = spriteSpec(c, " ".join(spec).replace(" ", ""))
    if not picks:
        die("nothing matching " + " ".join(spec))
    if not os.path.isdir(out):
        os.makedirs(out)
    written = []
    for label, sid in picks:
        src = os.path.join(SPRITES, "sprites-%d.png" % sid)
        if not os.path.exists(src):
            continue
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label) if label != str(sid) else str(sid)
        dst = os.path.join(out, "%s.png" % safe)
        shutil.copyfile(src, dst)
        written.append(dst)
    if not written:
        die("none of those %d sprites are extracted" % len(picks))
    for w in written[:10]:
        print("  %s" % w)
    if len(written) > 10:
        print("  ... %d files in total" % len(written))
    if show:
        os.startfile(written[0])


def showSprite(c, word):
    if word.lstrip("-").isdigit():
        sid = int(word)
        f = os.path.join(SPRITES, "sprites-%d.png" % sid)
        real = c.execute("select name from sprite_name where sprite_id=?", (sid,)).fetchone()
        print("  sprite %d" % sid)
        print("    name       %s" % (real[0] if real and real[0] else "not recovered, only a hash"))
        print("    file       %s" % (f if os.path.exists(f) else "not extracted"))
        for src, ot, oid, on in c.execute(
                "select source,owner_type,owner_id,owner_name from sprite_use where sprite_id=?"
                " order by (source='iface'), owner_type, owner_id", (sid,)):
            print("    %-40s %s %s" % (src, ot, on or oid))
        return
    picks = spriteSpec(c, word)
    if not picks:
        print("  nothing matching %s. A sprite answers to a name only if the hash was cracked or"
              " something points at it, the rest are id only." % word)
    for label, sid in picks[:40]:
        f = os.path.join(SPRITES, "sprites-%d.png" % sid)
        print("    %-8d %-52s %s" % (sid, label, "" if os.path.exists(f) else "(not extracted)"))


def showSheet(c, argv):
    """Lay the chosen sprites out on one labelled png, the way _all-runes.png came out. The sheet
    is a reference image, not an asset: -copy also drops the originals next to it, untouched."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        die("this one needs Pillow: python -m pip install pillow")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sheet.png")
    copy = show = False
    spec = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-o", "--out") and i + 1 < len(argv):
            out = argv[i + 1]
            i += 2
        elif a in ("-copy", "--copy"):
            copy = True
            i += 1
        elif a in ("-show", "--show"):
            show = True
            i += 1
        else:
            spec.append(a)
            i += 1
    if not spec:
        die("sheet wants ids, label=id pairs, or some text to match")

    picks = spriteSpec(c, " ".join(spec).replace(" ", ""))
    if not picks:
        die("nothing matching " + " ".join(spec))

    have = [(lab, sid) for lab, sid in picks if os.path.exists(os.path.join(SPRITES, "sprites-%d.png" % sid))]
    gone = [sid for lab, sid in picks if not os.path.exists(os.path.join(SPRITES, "sprites-%d.png" % sid))]
    if not have:
        die("none of those %d sprites are extracted under %s" % (len(picks), SPRITES))

    ims = [(lab, sid, Image.open(os.path.join(SPRITES, "sprites-%d.png" % sid)).convert("RGBA")) for lab, sid in have]
    # Cell is the biggest sprite in the set, but capped: one 456px banner in with a pile of 32px
    # icons would otherwise blow every cell up to 456 and give a sheet thousands of pixels wide.
    # Anything over the cap is thumbnailed, which is what you want to look at anyway.
    CAP = 128
    cell = max(64, min(CAP, max(max(im.width, im.height) for _, _, im in ims)))
    scaled = []
    for lab, sid, im in ims:
        if im.width > cell or im.height > cell:
            im = im.copy()
            im.thumbnail((cell, cell), Image.LANCZOS)
        scaled.append((lab, sid, im))
    ims = scaled
    # a sheet wider than it is tall reads better, and 7 across is what the runes wanted
    # A set fetched by name usually shares a long prefix, and trimming the tail then throws away the
    # only part that differs -- njcapes_shard_agility and njcapes_shard_mining both read njcapes_..
    # So lift the shared prefix out into a heading and label the tiles with what is left.
    labels = [lab for lab, sid, im in ims if lab != str(sid)]
    prefix = ""
    if len(labels) > 1:
        # case insensitive, because a match mixes cracked names (lowercase) with the component
        # names that draw them (upper), and those two spellings share everything but their case
        n = len(os.path.commonprefix([l.lower() for l in labels]))
        prefix = labels[0][:n]
        prefix = prefix[: prefix.rfind("_") + 1] if "_" in prefix else prefix
        if len(prefix) < 4 or len(prefix) >= min(len(l) for l in labels):
            prefix = ""

    cols = min(len(ims), max(4, int(len(ims) ** 0.5 * 1.4)))
    rows = (len(ims) + cols - 1) // cols
    pad, lab_h = 10, 26
    head = 20 if prefix else 0
    sheet = Image.new("RGBA", (cols * (cell + pad) + pad, head + rows * (cell + pad + lab_h) + pad), (26, 26, 30, 255))
    d = ImageDraw.Draw(sheet)
    if prefix:
        d.text((pad, 6), "%s*" % prefix, fill=(150, 150, 160, 255))

    def fit(text, width):
        """Elide the middle, not the end. These names carry their meaning in the tail, so cutting
        the tail is the one thing that must not happen. Measured, since the font is not fixed."""
        if d.textlength(text) <= width:
            return text
        lo, hi = text, ""
        while lo and d.textlength(lo + ".." + hi) > width:
            if len(hi) < len(lo) // 2:
                hi = lo[-1] + hi
            lo = lo[:-1]
        return lo + ".." + hi

    for n, (label, sid, im) in enumerate(ims):
        r, cnum = divmod(n, cols)
        x = pad + cnum * (cell + pad)
        y = head + pad + r * (cell + pad + lab_h)
        sheet.alpha_composite(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2))
        if label == str(sid):
            d.text((x + 2, y + cell + 3), str(sid), fill=(200, 200, 205, 255))
        else:
            short = label[len(prefix):] if prefix and label.startswith(prefix) else label
            d.text((x + 2, y + cell + 2), fit(short, cell), fill=(215, 215, 220, 255))
            d.text((x + 2, y + cell + 13), str(sid), fill=(130, 130, 140, 255))
    sheet.save(out)

    print("  %d sprites, %d across, cell %dpx" % (len(ims), cols, cell))
    print("  wrote %s" % out)
    if gone:
        print("  not extracted, skipped: %s" % ", ".join(str(g) for g in gone[:10]))

    if copy:
        dest = os.path.splitext(out)[0]
        if not os.path.isdir(dest):
            os.makedirs(dest)
        for label, sid, _ in ims:
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label) or str(sid)
            shutil.copyfile(os.path.join(SPRITES, "sprites-%d.png" % sid), os.path.join(dest, "%s.png" % safe))
        print("  originals copied to %s" % dest)

    if show:
        os.startfile(out)


USAGE = """  cache npc|loc|obj|struct|enum|quest|seq|iface <id | CONFIG_NAME | display name>  [-v]
  cache find <text>                  every kind
  cache find loc <text>              just that one
  cache var <VARNAME | id>
  cache shop <INVNAME | id>          shop stock out of invtype
  cache param <id | NAME>            a param, its type and who carries it
  cache anim <id | NAME>             a sequence, its frames and what plays it
  cache sprite <id | text>           what a sprite is and what points at it
  cache grab <ids | text> [-show] [-o dir]   just the png, no sheet
  cache sheet <ids | label=id | text> [-show] [-copy] [-o out.png]
  cache sql <select ...>

  -v     also dump the raw cache fields
  -show  open the sheet once it is written
  -copy  drop the originals in a folder beside it, named by label"""


def main(argv):
    if not argv:
        print(USAGE)
        return
    head, rest = argv[0].lower(), argv[1:]
    c = connect()

    if head in KINDS:
        if not rest:
            die("%s wants an id or a name" % head)
        verbose = "-v" in rest
        rest = [r for r in rest if r != "-v"]
        showConfig(c, head, " ".join(rest), verbose)
    elif head == "find":
        if not rest:
            die("find wants some text, with a kind in front of it to narrow things")
        if rest[0].lower() in KINDS and len(rest) > 1:
            showFind(c, rest[0].lower(), " ".join(rest[1:]))
        else:
            showFind(c, None, " ".join(rest))
    elif head == "var":
        if not rest:
            die("var wants a name or an id")
        showVar(c, " ".join(rest))
    elif head == "grab":
        grabSprite(c, rest)
    elif head == "sheet":
        showSheet(c, rest)
    elif head == "sprite":
        if not rest:
            die("sprite wants an id or some text")
        showSprite(c, " ".join(rest))
    elif head == "shop":
        if not rest:
            die("shop wants an inv id or a name")
        showShop(c, " ".join(rest))
    elif head == "param":
        if not rest:
            die("param wants an id or a name")
        showParam(c, " ".join(rest))
    elif head == "anim":
        if not rest:
            die("anim wants an id or a name")
        showConfig(c, "seq", " ".join(rest))
    elif head == "sql":
        q = " ".join(rest)
        if not q.lower().lstrip().startswith("select"):
            die("sql takes a select")
        cur = c.execute(q)
        cols = [d[0] for d in cur.description]
        print("  " + " | ".join(cols))
        for row in cur.fetchmany(60):
            print("  " + " | ".join(str(v)[:80] for v in row))
    else:
        # Anything that is not a subcommand is what you are looking for
        showFind(c, None, " ".join(argv))


if __name__ == "__main__":
    main(sys.argv[1:])
