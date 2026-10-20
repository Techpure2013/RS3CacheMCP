"""Clientscript names, checked against the code and cracked from each script's context.

The index keeps a name only as the Java hash of `[clientscript,x]` or `[proc,x]`, and about a third
of rs-name-hashes.json were collisions, not Jagex's names. A clientscript runs as a hook and a proc
is called, so a name of the wrong kind is dropped; one sharing no word with its context is unchecked.

A name list an add-on offers comes first, still kind-tested. New names come from stems of a script's neighbours, callers and hooking interfaces plus one word,
taken only as the one hit whose kind fits. hash(p + s) = hash(p) * 31^len(s) + hash(s).

    python -m rs3mcp.scriptnames [folder]

Headers become `//[proc,x]`, `//?[proc,x]` when unchecked, or `//`; script-names.json says how.
"""

import collections
import io
import json
import os
import re
import sqlite3
import sys

from . import cachefile, clientscript as cs, common

M = 0xFFFFFFFF
NEAR = 40
DICTIONARY = os.path.join(os.path.dirname(cs.NAMES), "rs-name-hashes.json")
FILE = re.compile(r"clientscript-(\d+)\.ts$")
NAME = re.compile(r"\[(clientscript|proc),(.+)\]$")

POW = [1]
for _ in range(256):
    POW.append((POW[-1] * 31) & M)


def jhash(s, h=0):
    for ch in s:
        h = (h * 31 + ord(ch)) & M
    return h


def parts(name):
    """(kind, body) of `[kind,body]`, else (None, None)."""
    m = NAME.match(name or "")
    return m.groups() if m else (None, None)


class Scripts:
    """What the decompiled scripts and the database say about who runs whom."""

    def __init__(self, folder, db_path=None):
        self.texts = {}
        for f in os.listdir(folder):
            m = FILE.match(f)
            if m:
                with io.open(os.path.join(folder, f), encoding="utf-8") as fh:
                    self.texts[int(m.group(1))] = fh.read()
        self.calls = collections.defaultdict(set)
        self.called, self.hooked = set(), set()
        for sid, text in self.texts.items():
            self.hooked.update(int(c) for c in re.findall(r"callback\(script(\d+)", text))
            for c in map(int, re.findall(r"(?<!callback\()\bscript(\d+)\(", text)):
                if c != sid:
                    self.called.add(c)
                    self.calls[sid].add(c)
                    self.calls[c].add(sid)
        self.ifaces = collections.defaultdict(set)
        self.iface_name = {}
        self.words = set()
        db_path = db_path or common.DB
        if os.path.exists(db_path):
            self._from_db(sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True))

    def _from_db(self, db):
        self.iface_name = {i: n.lower() for i, n in
                           db.execute("select id, name from config where type='Interface' and name is not null")}
        for cid, data in db.execute("select id, data from config where type='interfaces' and data like '%hooks%'"):
            for hook in (json.loads(data).get("hooks") or {}).values():
                if isinstance(hook, list) and hook and isinstance(hook[0], dict) and hook[0].get("kind") == 0:
                    self.ifaces[hook[0].get("int")].add(cid >> 16)
                    self.hooked.add(hook[0].get("int"))
        for (n,) in db.execute("select name from config where name is not null and type in "
                               "('Interface','VarPlayer','VarClient','StyleSheet','Inv','Quest','enums',"
                               "'dbtables','params')"):
            self.words.update(w for w in re.split(r"[^a-z0-9]+", n.lower()) if w)

    def kind_fits(self, sid, name):
        """True or False when the code says which kind the script is, None when it does not."""
        kind = parts(name)[0]
        if sid in self.called and sid not in self.hooked:
            return kind == "proc"
        if sid in self.hooked and sid not in self.called:
            return kind == "clientscript"
        return None

    def stems(self, sid, names):
        """Name stems the script's context suggests: `a`, `a_b`, ... of every related name."""
        out = set()
        for other in list(range(sid - NEAR, sid + NEAR + 1)) + sorted(self.calls.get(sid, ())):
            body = parts(names.get(other))[1] if other != sid else None
            if body:
                bits = body.split("_")
                out.update("_".join(bits[:k]) for k in range(1, len(bits)))
        out.update(self.iface_name[i] for i in self.ifaces.get(sid, ()) if i in self.iface_name)
        return out


def _suffixes(words):
    """{(length, hash): [suffix]} for `_word]`, and `]` alone."""
    table = collections.defaultdict(list)
    for s in ["]"] + ["_" + w + "]" for w in words]:
        table[(len(s), jhash(s))].append(s)
    return table


def crack(targets, names, scripts, table):
    """{script: name} where one candidate matches its hash."""
    lengths = sorted({k[0] for k in table})
    found = {}
    for sid, h in targets.items():
        hits = set()
        for stem in scripts.stems(sid, names):
            for kind in ("clientscript", "proc"):
                head = "[%s,%s" % (kind, stem)
                hh = jhash(head)
                for n in lengths:
                    for s in table.get((n, (h - hh * POW[n]) & M), ()):
                        hits.add(head + s)
        if len(hits) == 1:
            found[sid] = hits.pop()
    return found


def name_all(folder, root=None, db_path=None, say=print):
    """{script: (name, how)}, how being checked, cracked or unchecked."""
    desc = cachefile.descriptor(cs.MAJOR, root or cachefile.GAME) or {}
    target = {sid: g["nameHash"] & M for sid, g in desc.items() if g.get("nameHash") is not None}
    with io.open(DICTIONARY, encoding="utf-8") as fh:
        known = {int(h): n for h, n in json.load(fh).items()}
    scripts = Scripts(folder, db_path)
    listed = {sid: known[h] for sid, h in target.items() if h in known and parts(known[h])[0]}
    kept = {sid: n for sid, n in listed.items() if scripts.kind_fits(sid, n) is not False}
    say("clientscript names: %d in the dictionary, %d contradicted by the code"
        % (len(listed), len(listed) - len(kept)))

    words = set(scripts.words) | {str(k) for k in range(1000)}
    for n in known.values():
        body = parts(n)[1] or n
        bits = [w for w in re.split(r"[^a-z0-9]+", body.lower()) if w]
        words.update(bits)
        words.update("_".join(bits[k:k + 2]) for k in range(len(bits) - 1))
    table = _suffixes(words)
    names, how = dict(kept), {}
    for sid, n in listed_names(target).items():
        if scripts.kind_fits(sid, n) is not False:
            names[sid], how[sid] = n, "listed"
    while True:
        todo = {sid: h for sid, h in target.items() if sid not in names}
        got = {sid: n for sid, n in crack(todo, names, scripts, table).items()
               if scripts.kind_fits(sid, n) is True}
        if not got:
            break
        names.update(got)
        how.update({sid: "cracked" for sid in got})
    for sid, n in kept.items():
        if how.get(sid) == "listed":
            continue
        first = parts(n)[1].split("_")[0]
        near = scripts.stems(sid, names)
        supported = scripts.kind_fits(sid, n) and any(s == first or s.startswith(first) for s in near)
        how[sid] = "checked" if supported else "unchecked"
    # context cracks replace unchecked names
    doubtful = {sid: target[sid] for sid, v in how.items() if v == "unchecked"}
    for sid, n in crack(doubtful, names, scripts, table).items():
        if n != names[sid] and scripts.kind_fits(sid, n) is True:
            names[sid], how[sid] = n, "cracked"
    say("clientscript names: %d listed, %d checked, %d cracked, %d unchecked, of %d scripts"
        % tuple([sum(1 for v in how.values() if v == k) for k in ("listed", "checked", "cracked", "unchecked")]
                + [len(target)]))
    return {sid: (names[sid], how[sid]) for sid in names}


def listed_names(target):
    """{script: name} from name lists an add-on offers, by hash. A list may cover other builds."""
    from . import addons
    by_hash = {}
    for path in addons.first("script_name_lists") or ():
        with io.open(path, encoding="utf-8") as fh:
            for line in fh:
                if parts(line.strip())[0]:
                    by_hash.setdefault(jhash(line.strip()), line.strip())
    return {sid: by_hash[h] for sid, h in target.items() if h in by_hash}


def write_headers(folder, named):
    """Each script's first line to its name, and script-names.json beside them."""
    for f in os.listdir(folder):
        m = FILE.match(f)
        if not m:
            continue
        path = os.path.join(folder, f)
        with io.open(path, encoding="utf-8") as fh:
            text = fh.read()
        first, _, rest = text.partition("\n")
        name, how = named.get(int(m.group(1)), ("", None))
        want = "//%s%s" % ("?" if how == "unchecked" else "", name)
        if first.startswith("//") and first != want:
            with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(want + "\n" + rest)
    with io.open(os.path.join(folder, "script-names.json"), "w", encoding="utf-8") as fh:
        json.dump({str(sid): {"name": n, "how": h} for sid, (n, h) in sorted(named.items())}, fh, indent=0)


def run(folder, root=None, db_path=None, say=print):
    named = name_all(folder, root, db_path, say)
    write_headers(folder, named)
    return named


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else common.CLIENTSCRIPT)
