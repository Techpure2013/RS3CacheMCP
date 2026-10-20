"""Clientscripts back into readable code.

The instructions are cut into blocks at every jump, and each block runs on a symbolic stack that
turns pushes and pops into expressions and statements. Nothing is left on the stack across a jump
(none of the 209,244 block starts in the cache has a value waiting), so a block stands alone.
Branches with nothing between them fold into && and ||. Then the blocks are laid out as if, while
and switch, by address, the order the compiler wrote them in.

The layout is checked, not trusted. Where control goes after every block is worked out again from
the laid out tree and compared with the jumps. A script that disagrees anywhere is written flat,
as labels and gotos. A jump that fits no shape is a goto, and counted.

    python rs3mcp/csdecomp.py 1000          one script
    python rs3mcp/csdecomp.py check         every script: failures, gotos, layout disagreements
    python rs3mcp/csdecomp.py write <dir>   every script to files
"""

import collections
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rs3mcp import clientscript as cs, csstack as st, cstypes as ty, ops  # noqa: E402

I, L, S = st.I, st.L, st.S
OP = st.OP

BINARY = {4000: "+", 4001: "-", 4002: "*", 4003: "/", 4011: "%", 4014: "&", 10075: "|",
          10238: "+", 10322: "-", 10371: "*", 10412: "/", 10687: "%"}
LONG_MATH = {10238, 10322, 10371, 10412, 10687}
COMPARE = {7: "!=", 8: "==", 9: "<", 10: ">", 31: "<=", 32: ">=",
           10449: "!=", 10153: "==", 10411: "<", 10321: ">", 10491: "<=", 10349: ">="}
NEGATE = {"==": "!=", "!=": "==", "<": ">=", ">=": "<", "<=": ">", ">": "<="}
PREC = {"||": 20, "&&": 30, "==": 40, "!=": 40, "<": 40, ">": 40, "<=": 40, ">=": 40,
        "|": 50, "&": 60, "+": 70, "-": 70, "*": 80, "/": 80, "%": 80}
VAR_NAMES = {0: "varplayer", 1: "varnpc", 2: "varclient", 3: "varworld", 4: "varregion",
             5: "varobject", 6: "varclan", 7: "varclansettings", 9: "varplayergroup"}
ATOM = 100
END = -1
BUMP = re.compile(r"(\w+) = \1 \+ 1;")
# ops typed by the data they name
DYNAMIC = st.GETPARAM | {OP["enum"], OP["db_getfield"], OP["db_find"], OP["db_find_with_count"],
                         OP["db_find_733"]}


class Expr:
    __slots__ = ("text", "prec", "value", "cmp", "type", "slot")

    def __init__(self, text, prec=ATOM, value=None, cmp=None, typ=None, slot=None):
        self.text = text
        self.prec = prec
        # the constant this is, when it is one
        self.value = value
        # (left, symbol, right) when this is a comparison
        self.cmp = cmp
        # the script type it has, and its slot
        self.type = typ
        self.slot = slot

    def at(self, prec):
        return self.text if self.prec >= prec else "(%s)" % self.text


def binary(a, sym, b):
    p = PREC[sym]
    # left associative: equal precedence needs brackets right
    cmp = (a, sym, b) if sym in NEGATE else None
    return Expr("%s %s %s" % (a.at(p), sym, b.at(p + 1)), p, cmp=cmp)


def sink(e, t):
    """e meets a place of type t: a constant reads as that type, and where it came from votes."""
    if t is None:
        return
    Script.current_votes.vote(e.slot, t)
    if not ty.informative(t):
        return
    if e.value is not None and isinstance(e.value, int) and e.type is None:
        shown = ty.literal(e.value, t)
        if shown is not None:
            e.text = shown
            e.prec = 95 if " as " in shown else ATOM
    if e.type is None:
        e.type = t


def negate(c):
    if c.cmp is not None:
        a, sym, b = c.cmp
        return binary(a, NEGATE[sym], b)
    return Expr("!%s" % c.at(ATOM), 90)


def literal(kind, value):
    if kind == S:
        return Expr(json.dumps(value, ensure_ascii=False), value=value)
    if kind == L:
        return Expr("%dL" % value, value=value)
    return Expr(str(value), ATOM if value >= 0 else 90, value=value)


class Script:
    current_votes = None

    def __init__(self, ctx, sid, names):
        Script.current_votes = ctx.votes
        self.ctx, self.sid, self.names = ctx, sid, names
        self.code = ctx.code[sid]
        self.head = ctx.heads[sid]
        self.gotos = 0
        self.labels = set()
        self.args = {I: self.head["argInts"], S: self.head["argStrings"], L: self.head["argLongs"]}
        self.used_locals = set()
        self.tmp = 0
        # loops being laid out right now
        self.active = set()
        # blocks already laid out, for unreached regions
        self.laid = set()
        self.depth = 0
        # a short return written at each place
        self.inline_returns = False

    # blocks

    def cut(self):
        code, n = self.code, len(self.code)
        starts = {0}
        for i, (op, imm) in enumerate(code):
            if op == OP["jump"] or op in COMPARE:
                starts.update((i + 1, i + 1 + imm))
            elif op == OP["switch"]:
                starts.add(i + 1)
                starts.update(i + 1 + j for j in self.head["switches"][imm].values())
            elif op == OP["ret"]:
                starts.add(i + 1)
        starts = sorted(s for s in starts if s < n)
        self.order = starts
        self.blocks = {}
        for k, s in enumerate(starts):
            e = starts[k + 1] if k + 1 < len(starts) else n
            self.blocks[s] = self.run(s, e)

    def local(self, kind, index):
        name = {I: "int", S: "string", L: "long"}[kind] + str(index)
        if index >= self.args[kind]:
            self.used_locals.add((kind, index))
        return name

    def run(self, start, end):
        """(statements, terminator) for one block, from a symbolic stack.

        Every value carries the type it is known to have and the slot it came from. Where a
        value meets a slot of known type, the slot it came from gets a vote and a constant is
        written as that type; where a typed value lands in a slot, the slot gets a vote."""
        ctx, votes, sid = self.ctx, self.ctx.votes, self.sid
        stack, out, seq = [], [], [0]

        def push(kind, expr):
            seq[0] += 1
            stack.append((kind, expr, seq[0]))

        def take(kinds):
            got = []
            for kind in reversed(kinds):
                for j in range(len(stack) - 1, -1, -1):
                    if stack[j][0] == kind:
                        got.append(stack.pop(j))
                        break
                else:
                    raise st.Wrong("empty %s stack" % kind)
            # arguments read in the order they were pushed
            got.sort(key=lambda e: e[2])
            return [(e[0], e[1]) for e in got]

        def values(kinds):
            return [e for _, e in take(kinds)]

        def into(slot, e, t=None):
            """e flows into slot, whose type is t or whatever the slot has settled on."""
            t = t if t is not None else votes.get(slot)
            # the type e had coming in
            had = e.type
            sink(e, t)
            votes.vote(slot, had)

        def typed(text, slot=None, t=None, prec=ATOM):
            return Expr(text, prec, typ=t if t is not None else votes.get(slot), slot=slot)

        def positions(pairs):
            n = collections.Counter()
            for kind, e in pairs:
                yield kind, n[kind], e
                n[kind] += 1

        term = ("goto", end)
        for i in range(start, end):
            op, imm = self.code[i]
            if op == OP["const"]:
                push(imm[0], literal(*imm))
            elif op in (OP["push_int_local"], OP["push_string_local"], OP["push_long_local"]):
                kind = {OP["push_int_local"]: I, OP["push_string_local"]: S}.get(op, L)
                push(kind, typed(self.local(kind, imm), ("local", sid, kind, imm)))
            elif op in (OP["pop_int_local"], OP["pop_string_local"], OP["pop_long_local"]):
                kind = {OP["pop_int_local"]: I, OP["pop_string_local"]: S}.get(op, L)
                v, = values([kind])
                into(("local", sid, kind, imm), v)
                name = self.local(kind, imm)
                ints = [j for j, e in enumerate(stack) if e[0] == I]
                if i - 4 >= start and st._postfix(self.code, i) and ints and stack[ints[-1]][1].text == name:
                    # x++ as an argument, old x still waiting
                    j = ints[-1]
                    old = stack[j][1]
                    step = "++" if cs.names().get(self.code[i - 1][0]) == "ADD" else "--"
                    stack[j] = (I, Expr(name + step, typ=old.type, slot=old.slot), stack[j][2])
                else:
                    out.append("%s = %s;" % (name, v.text))
            elif op in (OP["discard_int"], OP["discard_string"], OP["discard_long"]):
                kind = {OP["discard_int"]: I, OP["discard_string"]: S}.get(op, L)
                v, = values([kind])
                out.append("%s;" % v.text)
            elif op in (OP["push_var"], OP["pop_var"]):
                dom, vid = imm >> 24, (imm >> 8) & 0xFFFF
                name = "%s_%d" % (VAR_NAMES.get(dom, "var%d" % dom), vid)
                vt = ctx.vars.get((dom, vid), 0)
                kind = st.prim(vt)
                if op == OP["push_var"]:
                    push(kind, typed(name, t=vt))
                else:
                    v, = values([kind])
                    sink(v, vt)
                    out.append("%s = %s;" % (name, v.text))
            elif op in (OP["push_varbit"], OP["pop_varbit"]):
                name = "varbitplayer_%d" % (imm >> 8)
                if op == OP["push_varbit"]:
                    push(I, Expr(name))
                else:
                    v, = values([I])
                    out.append("%s = %s;" % (name, v.text))
            elif op in BINARY:
                kind = L if op in LONG_MATH else I
                a, b = values([kind, kind])
                result = binary(a, BINARY[op], b)
                result.type = 0 if kind == I else None
                push(kind, result)
            elif op in COMPARE:
                kind = L if op in st.LONG_BRANCHES else I
                a, b = values([kind, kind])
                sink(a, b.type)
                sink(b, a.type)
                term = ("cond", binary(a, COMPARE[op], b), i + 1 + imm, i + 1)
            elif op == OP["jump"]:
                term = ("goto", i + 1 + imm)
            elif op == OP["ret"]:
                vals = values([e[0] for e in stack])
                for k, v in enumerate(vals):
                    into(("ret", sid, k), v)
                term = ("return", vals)
            elif op == OP["switch"]:
                v, = values([I])
                table = {val: i + 1 + j for val, j in self.head["switches"][imm].items()}
                term = ("switch", v, table, i + 1)
            elif op == OP["join"]:
                parts = values([S] * imm)
                push(S, Expr(" + ".join(p.at(71) for p in parts), 70))
            elif op == OP["gosub"]:
                h = ctx.heads[imm]
                args = take([I] * h["argInts"] + [S] * h["argStrings"] + [L] * h["argLongs"])
                rets = ctx.returns.get(imm)
                if rets is None:
                    raise st.Stuck(imm)
                for kind, n, e in positions(args):
                    into(("local", imm, kind, n), e)
                call = Expr("script%d(%s)" % (imm, ", ".join(e.text for _, e in args)))
                self.emit_call(out, call, list(rets), push, [("ret", imm, k) for k in range(len(rets))])
            elif op == OP["push_array"]:
                idx, = values([I])
                push(I, Expr("array%d[%s]" % (imm, idx.text)))
            elif op == OP["pop_array"]:
                idx, v = values([I, I])
                out.append("array%d[%s] = %s;" % (imm, idx.text, v.text))
            elif op == OP["define_array"]:
                size, = values([I])
                out.append("array%d = new_array(%s);" % (imm >> 16, size.text))
            else:
                consts = [(k, e.value) for k, e, _ in stack]
                pops, pushes = st.effect(ctx, op, imm, consts)
                name = self.opname(op, imm)
                shape = (ctx.fixed.get(op) or ctx.guess.get(op) or ([], []))[0]
                if "vararg" in shape:
                    call = self.hook(name, op, shape, consts, take, into)
                    self.emit_call(out, call, pushes, push, [])
                    continue
                args = take(pops)
                known = self.dynamic_types(op, args)
                dynamic = op in DYNAMIC
                for kind, n, e in positions(args):
                    if dynamic:
                        sink(e, known.get(("in", kind, n)))
                    else:
                        into(("op", op, "inT", kind, n), e, known.get(("in", kind, n)))
                call = Expr("%s(%s)" % (name, ", ".join(e.text for _, e in args)))
                slots, outs, n = [], [], collections.Counter()
                for kind in pushes:
                    slots.append(None if dynamic else ("op", op, "outT", kind, n[kind]))
                    outs.append(known.get(("out", kind, n[kind])))
                    n[kind] += 1
                self.emit_call(out, call, pushes, push, slots, outs)
        return self.tidy(out), term

    def dynamic_types(self, op, args):
        """Types an op takes or gives that come from the data rather than the op."""
        ctx = self.ctx
        ints = [e for k, e in args if k == I]
        got = {}
        if op in st.GETPARAM and ints and ints[-1].value is not None:
            got[("out", st.prim(ctx.params.get(ints[-1].value, 0)), 0)] = ctx.params.get(ints[-1].value)
            if op != st.CC_PARAM:
                got[("in", I, 0)] = {4500: 73, 4208: 33, 10262: 3, 10076: 32, 10192: None}.get(op)
        elif op == OP["enum"] and len(ints) == 4 and ints[0].value is not None and ints[1].value is not None:
            ktype, vtype = ints[0].value, ints[1].value
            for e, t in ((ints[0], None), (ints[1], None)):
                e.text = ty.name(e.value) if e.value in ty.NAMES else e.text
            got[("in", I, 2)] = 26
            got[("in", I, 3)] = ktype
            got[("out", st.prim(vtype), 0)] = vtype
        elif op == OP["db_getfield"] and len(ints) == 3 and ints[1].value is not None:
            col = ints[1].value
            types = ctx.columns.get((col >> 12, (col >> 4) & 0xFF)) or []
            if col & 0xF:
                types = types[(col & 0xF) - 1:col & 0xF]
            got[("in", I, 0)] = 74
            n = collections.Counter()
            for t in types:
                got[("out", st.prim(t), n[st.prim(t)])] = t
                n[st.prim(t)] += 1
        return got

    def tidy(self, out):
        """[tmp0, tmp1] = f(); tmp1; tmp0; is just f(), and [tmp0, tmp1] = f(); y = tmp1; x = tmp0;
        is [x, y] = f()."""
        out = self.gather(out)
        dropped = {s[:-1] for s in out if s.startswith("tmp") and s.endswith(";") and s[:-1][3:].isdigit()}
        if not dropped:
            return out
        tidy = []
        for s in out:
            if s[:-1] in dropped:
                continue
            if s.startswith("[tmp") and "] = " in s:
                names = s[1:s.index("]")].split(", ")
                if all(n in dropped for n in names):
                    s = s[s.index("] = ") + 4:]
            tidy.append(s)
        return tidy

    @staticmethod
    def gather(out):
        # a tmp is read once, so stores follow
        res, i = [], 0
        while i < len(out):
            s = out[i]
            if s.startswith("[tmp") and "] = " in s:
                names = s[1:s.index("]")].split(", ")
                into = {}
                for t in out[i + 1:i + 1 + len(names)]:
                    m = re.fullmatch(r"(\w+) = (tmp\d+);", t)
                    if not m or m.group(2) not in names or m.group(2) in into:
                        break
                    into[m.group(2)] = m.group(1)
                if len(into) == len(names) and len(set(into.values())) == len(names):
                    res.append("[%s] = %s" % (", ".join(into[n] for n in names), s[s.index("] = ") + 4:]))
                    i += 1 + len(names)
                    continue
            res.append(s)
            i += 1
        return res

    def emit_call(self, out, call, pushes, push, slots, types=None):
        votes = self.ctx.votes
        types = types or [None] * len(pushes)

        def result(text, k):
            slot = slots[k] if k < len(slots) else None
            t = types[k] if types[k] is not None else (votes.get(slot) if slot else None)
            return Expr(text, call.prec if text is call.text else ATOM, typ=t, slot=slot)

        if slots and len(slots) < len(pushes):
            slots = slots + [None] * (len(pushes) - len(slots))
        if not slots:
            slots = [None] * len(pushes)

        if not pushes:
            out.append(call.text + ";")
        elif len(pushes) == 1:
            push(pushes[0], result(call.text, 0))
        else:
            # several values at once, named in order
            names = ["tmp%d" % (self.tmp + k) for k in range(len(pushes))]
            self.tmp += len(pushes)
            out.append("[%s] = %s;" % (", ".join(names), call.text))
            for k, (tmp, kind) in enumerate(zip(names, pushes)):
                push(kind, result(tmp, k))

    def hook(self, name, op, shape, consts, take, into):
        """callback(script, args...), the trigger list when the type string ends in Y, then
        whatever the op takes besides."""
        at = shape.index("vararg")
        after = shape[at + 1:]
        sig = [c for k, c in consts if k == S][-1]
        triggers = sig.endswith("Y")
        body = sig[:-1] if triggers else sig
        count = [c for k, c in consts if k == I][-1 - after.count(I)] if triggers else 0
        kinds = shape[:at] + [st.sigkind(ch) for ch in body] \
            + [I] * (count + (1 if triggers else 0)) + [S] + after
        vals = [e for _, e in take(kinds)]
        pre, rest = vals[:at], vals[at:]
        args = rest[:len(body)]
        trig = rest[len(body):len(body) + count]
        tail = rest[len(body) + count + (1 if triggers else 0) + 1:]
        script = pre[-1] if pre else None
        callee = script.value if script is not None and isinstance(script.value, int) else None
        if callee is not None and callee in self.ctx.heads:
            n = collections.Counter()
            for ch, e in zip(body, args):
                kind = st.sigkind(ch)
                into(("local", callee, kind, n[kind]), e)
                n[kind] += 1
        for k, e in enumerate(tail):
            into(("hook", op, "tail", k), e)
        for k, e in enumerate(pre[:-1]):
            into(("hook", op, "pre", k), e)
        head = "script%d" % callee if callee is not None and callee >= 0 else (script.text if script else "-1")
        parts = ["callback(%s)" % ", ".join([head] + [a.text for a in args])]
        if triggers:
            parts.append("[%s]" % ", ".join(t.text for t in trig))
        parts += [t.text for t in tail] + [p.text for p in pre[:-1]]
        return Expr("%s(%s)" % (name, ", ".join(parts)))

    def opname(self, op, imm):
        name = self.names.get(op)
        name = name.lower() if name else "op%d" % op
        kind = self.ctx.opmap_kinds.get(op)
        if kind == "byte" and imm == 1 and name.startswith("cc_"):
            return "." + name
        if kind in ("byte", "int", "tribyte") and imm not in (0, None) and not name.startswith("cc_"):
            return "%s<%d>" % (name, imm)
        return name

    # control flow

    def graph(self):
        blocks = self.blocks
        reach, work = set(), [0]
        while work:
            b = work.pop()
            if b in reach or b not in blocks:
                continue
            reach.add(b)
            work.extend(self.succ(b))
        self.reach = reach
        self.forward = {}
        for b in sorted(reach):
            stmts, term = blocks[b]
            if not stmts and term[0] == "goto" and term[1] != b:
                self.forward[b] = term[1]
        self.fold()
        self.loops = {}
        for b in sorted(reach):
            if b in self.merged or b in self.forward:
                continue
            for s in self.succ(b):
                s = self.follow(s)
                if s <= b:
                    self.loops[s] = max(self.loops.get(s, s), b)

    def succ(self, b):
        term = self.blocks[b][1]
        if term[0] == "goto":
            return [term[1]] if term[1] < len(self.code) else []
        if term[0] == "cond":
            return [term[2], term[3]]
        if term[0] == "switch":
            return list(term[2].values()) + [term[3]]
        return []

    def follow(self, b):
        seen = set()
        while b in self.forward and b not in seen:
            seen.add(b)
            b = self.forward[b]
        return b if b < len(self.code) else END

    def fold(self):
        """a || b and a && b, from a conditional whose other branch is a bare conditional."""
        self.merged = set()
        changed = True
        while changed:
            changed = False
            # a merge takes edges away, so recount them
            counts = collections.Counter()
            for b in self.reach:
                if b in self.forward or b in self.merged:
                    continue
                for s in self.succ(b):
                    counts[self.follow(s)] += 1
            for b in sorted(self.reach):
                if b in self.merged or b in self.forward:
                    continue
                stmts, term = self.blocks[b]
                if term[0] != "cond":
                    continue
                c, t, f = term[1], self.follow(term[2]), self.follow(term[3])
                for y in (f, t):
                    if y == b or y == END or y in self.merged or counts[y] != 1:
                        continue
                    ys, yt = self.blocks[y]
                    if ys or yt[0] != "cond":
                        continue
                    yc, ytt, yff = yt[1], self.follow(yt[2]), self.follow(yt[3])
                    new = None
                    if y == f and ytt == t:
                        new = (binary(c, "||", yc), t, yff)
                    elif y == f and yff == t:
                        new = (binary(c, "||", negate(yc)), t, ytt)
                    elif y == t and yff == f:
                        new = (binary(c, "&&", yc), ytt, f)
                    elif y == t and ytt == f:
                        new = (binary(c, "&&", negate(yc)), yff, f)
                    if new:
                        self.blocks[b] = (stmts, ("cond", new[0], new[1], new[2]))
                        self.merged.add(y)
                        changed = True
                        break

    def next_after(self, b):
        k = self.order.index(b)
        return self.order[k + 1] if k + 1 < len(self.order) else len(self.code)

    def last_block(self, start, end):
        inside = [b for b in self.order if start <= b < end and b in self.reach
                  and b not in self.merged and b not in self.forward]
        return inside[-1] if inside else None

    def region(self, start, end, loop, natural=None):
        """Nodes for the blocks from start up to end, in address order.

        natural is the set of jump targets that mean the same as falling off the end of this
        region. By default that is just end; a branch whose if sits at the tail of its parent
        also has the parent's, and a loop body has its header."""
        self.depth += 1
        if self.depth > 400:
            raise RecursionError("nesting past 400 at %d" % start)
        try:
            return self._region(start, end, loop, {end} if natural is None else set(natural))
        finally:
            self.depth -= 1

    def _region(self, start, end, loop, natural):
        out = []
        cur = self.follow(start)
        while True:
            if cur is None or cur == END or not (start <= cur < end) or cur in self.laid:
                # this path is over, later blocks still ours
                cur = self.unlaid(start, end)
                if cur is None:
                    break
            if cur in self.loops and cur not in self.active:
                cur = self.loop(cur, out, end, natural, loop)
                continue
            self.laid.add(cur)
            stmts, term = self.blocks[cur]
            out.append(("stmts", cur, stmts))
            kind = term[0]
            if kind == "return":
                out.append(("return", cur, self.return_text(term[1])))
                cur = None
            elif kind == "goto":
                cur = self.jump(self.follow(term[1]), cur, end, loop, out, natural)
            elif kind == "cond":
                cur = self.cond(cur, term, end, loop, out, natural)
            else:
                cur = self.switch(cur, term, end, loop, out, natural)
        return out

    def after_loop(self, exit_, head, end, loop_outer, out, natural):
        """Carry on after a loop: at its exit when that is later in this region."""
        if exit_ == END or exit_ in natural:
            return None
        if exit_ >= end or exit_ <= head:
            # leaving the loop may break or continue outside
            return self.jump(exit_, head, end, loop_outer, out, natural)
        return exit_

    def unlaid(self, start, end):
        for b in self.order:
            if start <= b < end and b in self.reach and b not in self.laid \
                    and b not in self.merged and b not in self.forward:
                return b
        return None

    def jump(self, t, cur, end, loop, out, natural):
        """Where a plain jump leaves the reader: onward, nothing, a break, a continue, a goto.

        Returns the block to carry on laying out, or None when this path of the region ends."""
        if t in natural:
            return None
        if loop is not None:
            if t == loop[0] and loop[3] == "loop":
                out.append(("continue",))
                return None
            if t == loop[2]:
                out.append(("break",))
                return None
            # a continue reaches through switches to its loop
            outer = loop[4]
            while outer is not None and loop[3] == "switch":
                if outer[3] == "loop":
                    if t == outer[0]:
                        out.append(("continue",))
                        return None
                    break
                outer = outer[4]
        if t != END and cur < t < end:
            return t
        return self.goto(t, out)

    def goto(self, t, out):
        if t != END and t in self.blocks and self.blocks[t][1][0] == "return" and len(self.blocks[t][0]) <= 2:
            # a jump to a short returning block
            out.append(("goto", t, self.blocks[t][0] + [self.return_text(self.blocks[t][1][1])]))
            return None
        self.gotos += 1
        self.labels.add(t)
        out.append(("goto", t))
        return None

    def shared_return(self, b):
        """A block of at most two statements and a return, which more than one jump reaches."""
        if b == END or b not in self.blocks:
            return False
        stmts, term = self.blocks[b]
        if term[0] != "return" or len(stmts) > 2:
            return False
        return self._into(b) > 1

    def shared_step(self, b):
        """Where a block of at most two statements that jumps on goes, when more than one jump
        reaches it; else None."""
        if b == END or b not in self.blocks or b in self.forward:
            return None
        stmts, term = self.blocks[b]
        if term[0] != "goto" or len(stmts) > 2 or not stmts or self._into(b) < 2:
            return None
        join = self.follow(term[1])
        return None if join == END or join <= b else join

    def _into(self, b):
        return sum(1 for blk in self.reach if blk not in self.merged and blk not in self.forward
                   for s in self.succ(blk) if self.follow(s) == b)

    @staticmethod
    def return_text(vals):
        return "return;" if not vals else "return %s;" % (
            vals[0].text if len(vals) == 1 else "[%s]" % ", ".join(v.text for v in vals))

    def cond(self, cur, term, end, loop, out, natural):
        c, t, f = term[1], self.follow(term[2]), self.follow(term[3])
        if loop is not None and loop[3] == "loop":
            for target, is_t in ((t, True), (f, False)):
                if target in (loop[0], loop[2]) and target not in natural:
                    out.append(("if", cur, c if is_t else negate(c), is_t,
                                [("break",) if target == loop[2] else ("continue",)], None))
                    other = f if is_t else t
                    return self.jump(other, cur, end, loop, out, natural) if other in natural \
                        or other == END or not (cur < other < end) else other
        def leaves(x):
            return x == END or x in natural or not (cur < x < end)

        if self.inline_returns:
            for target, is_t in ((t, True), (f, False)):
                other = f if is_t else t
                if self.shared_return(target):
                    self.laid.add(target)
                    ret = ("goto", target, self.blocks[target][0] + [self.return_text(self.blocks[target][1][1])])
                    out.append(("if", cur, c if is_t else negate(c), is_t, [ret], None))
                    return self.jump(other, cur, end, loop, out, natural) if leaves(other) else other
                join = self.shared_step(target)
                if join is not None and cur < other < join <= end and (join == end or join not in natural):
                    # a short shared block that jumps on
                    self.laid.add(target)
                    other_nodes = self.region(other, join, loop, natural={join})
                    out.append(("if", cur, c if is_t else negate(c), is_t,
                                [("inline", target, self.blocks[target][0])], other_nodes))
                    return None if join == end else join

        if leaves(t) and leaves(f):
            # neither side stays: say where each goes
            then = []
            self.jump(t, cur, end, loop, then, natural)
            out.append(("if", cur, c, True, then, None))
            return self.jump(f, cur, end, loop, out, natural)
        if leaves(t) or leaves(f):
            near, far = (f, t) if leaves(t) else (t, f)
        else:
            near, far = (t, f) if t < f else (f, t)
        is_t = near == t
        test = c if is_t else negate(c)

        def after(target):
            # what reaching target means inside a branch
            return {target} | (natural if target in natural else set())

        if leaves(far):
            # the other side leaves, so write it after
            then = self.region(near, end, loop, natural=after(far))
            out.append(("if", cur, test, is_t, then, None))
            return self.jump(far, cur, end, loop, out, natural)
        else_end = None
        last = self.last_block(near, far)
        if last is not None:
            lterm = self.blocks[last][1]
            if lterm[0] == "goto":
                jt = self.follow(lterm[1])
                if jt != END and far < jt <= end and jt not in natural \
                        and not (loop is not None and jt in (loop[0], loop[2])):
                    else_end = jt
                elif jt == END or jt > end or jt in natural or \
                        (loop is not None and jt in (loop[0], loop[2])):
                    tail = self.last_block(far, end)
                    tterm = self.blocks[tail][1] if tail is not None else None
                    if tterm is not None and tterm[0] == "goto" and self.follow(tterm[1]) == jt:
                        # both sides leave the same way: if/else
                        then = self.region(near, far, loop, natural=after(jt))
                        other = self.region(far, end, loop, natural=after(jt))
                        out.append(("if", cur, test, is_t, then, other))
                        return self.jump(jt, cur, end, loop, out, natural)
                    if jt in natural:
                        # the taken side ends here, rest is else
                        then = self.region(near, far, loop, natural=after(jt))
                        other = self.region(far, end, loop, natural=natural)
                        out.append(("if", cur, test, is_t, then, other))
                        return None
        if else_end is None and (last is None or self.blocks[last][1][0] != "goto"
                                 or self.follow(self.blocks[last][1][1]) <= last):
            # the taken side ends in a loop
            outs = set()
            for blk in self.order:
                if near <= blk < far and blk in self.reach and blk not in self.merged \
                        and blk not in self.forward:
                    outs.update(x for x in map(self.follow, self.succ(blk)) if not near <= x < far)
            jt = outs.pop() if len(outs) == 1 else None
            if jt is not None and jt != END and far < jt <= end and jt not in natural \
                    and not (loop is not None and jt in (loop[0], loop[2])):
                else_end = jt
            elif jt is not None and jt in natural:
                then = self.region(near, far, loop, natural=after(jt))
                other = self.region(far, end, loop, natural=natural)
                out.append(("if", cur, test, is_t, then, other))
                return None
        nxt = else_end if else_end is not None else far
        inside = {nxt} | (natural if nxt == end else set())
        then = self.region(near, far, loop, natural=inside)
        other = self.region(far, else_end, loop, natural=inside) if else_end is not None else None
        out.append(("if", cur, test, is_t, then, other))
        return None if nxt == end else nxt

    def switch(self, cur, term, end, loop, out, natural):
        v, table, default = term[1], term[2], self.follow(term[3])
        by_target = collections.defaultdict(list)
        for val, tgt in sorted(table.items()):
            by_target[self.follow(tgt)].append(val)
        starts = sorted(s for s in by_target if s != END and cur < s < end)
        # a match straight out has no code here
        away = sorted(s for s in by_target if s != END and s not in starts)
        # where the cases meet again, by vote
        ends = collections.Counter(s for s in away if s > cur)
        # default code votes too, sitting before the cases
        own = [default] if default != END and default not in by_target and cur < default < end else []
        for s in starts + own:
            # a case runs to the next one
            bound = min([x for x in starts + [default] if x != END and x > s] + [end])
            outs = set()
            for blk in self.order:
                if s <= blk < bound and blk in self.reach and blk not in self.merged \
                        and blk not in self.forward:
                    outs.update(x for x in map(self.follow, self.succ(blk)) if x == END or x >= bound)
            for j in outs:
                ends[j] += 1
        if not ends and starts and default != END and cur < default and default > max(starts):
            # no case ends in a jump
            past = collections.Counter()
            for blk in self.order:
                if starts[0] <= blk < default and blk in self.reach and blk not in self.merged \
                        and blk not in self.forward:
                    for x in (self.follow(s) for s in self.succ(blk)):
                        if x != END and x > default:
                            past[x] += 1
            ends[min(past, key=lambda x: (-past[x], x)) if past else default] += 1
        # nothing in the switch goes on past it
        never = not ends
        meet = min(ends, key=lambda x: (-ends[x], x)) if ends else end
        follow = end if meet == END else min(meet, end)
        # the default is code of its own
        has_default = default not in (meet, END) and default not in by_target and cur < default < follow
        regions = sorted(starts + ([default] if has_default else []))
        bounds = regions + [follow]
        inner = (cur, cur, meet, "switch", loop)
        cases = []
        for k, s in enumerate(regions):
            values = None if has_default and s == default else by_target[s]
            cases.append((values, self.region(s, bounds[k + 1], inner)))
        for s in away:
            leave = []
            self.jump(s, cur, follow, inner, leave, {follow} if meet == follow else set())
            cases.append((by_target[s], leave))
            regions = regions + [s]
        if default not in by_target and not has_default and default != meet:
            # no match leaves the switch elsewhere
            leave = []
            self.jump(default, cur, follow, inner, leave, {follow} if meet == follow else set())
            cases.append((None, leave))
            regions = regions + [None]
        if default in by_target:
            # no match runs a case's own code
            cases = [(vals + ["default"] if vals is not None and s == default else vals, body)
                     for (vals, body), s in zip(cases, regions)]
        out.append(("switch", cur, v, cases, None, END in by_target))
        if never:
            return None
        if meet == END or meet >= end:
            return self.jump(meet if meet == END else self.follow(meet), cur, end, loop, out, natural)
        return follow

    def loop(self, head, out, end, natural, outer):
        latch = self.loops[head]
        # the jump back can sit past the latch
        tail = max([latch] + [f for f in self.forward if f > latch and f in self.reach
                              and self.follow(f) == head])
        body_end = self.next_after(tail)
        stmts, term = self.blocks[head]
        # the exit is the nearest place ahead
        outside = set()
        for blk in self.order:
            if head <= blk < body_end and blk in self.reach and blk not in self.merged \
                    and blk not in self.forward:
                for s in self.succ(blk):
                    x = self.follow(s)
                    if x == END or x >= body_end or x < head:
                        outside.add(x)
        ahead = [x for x in outside if x != END and x >= body_end]
        # a jump back out is the way out
        back = [x for x in outside if x != END and x < head]
        exit_ = min(ahead) if ahead else max(back) if back else END if outside else body_end
        # a header of one increment reads as ++x
        bump = BUMP.fullmatch(stmts[0]) if len(stmts) == 1 else None
        plain = term[0] == "cond" and (not stmts or bump is not None)
        if plain:
            for x in (self.follow(term[2]), self.follow(term[3])):
                if x == END or x >= body_end or x == exit_:
                    exit_ = x
                    break
        inner = (head, latch, exit_, "loop", outer)
        self.active.add(head)
        try:
            if plain:
                c, t, f = term[1], self.follow(term[2]), self.follow(term[3])
                test = c if f == exit_ else negate(c)
                if bump is not None:
                    uses = re.findall(r"\b%s\b" % bump.group(1), test.text)
                    test = Expr(re.sub(r"\b%s\b" % bump.group(1), "++" + bump.group(1), test.text, count=1),
                                test.prec) if len(uses) == 1 else None
                if exit_ in (t, f) and test is not None:
                    self.laid.add(head)
                    is_t = f == exit_
                    body = self.region(t if is_t else f, body_end, inner, natural={head})
                    out.append(("while", head, test, is_t, body))
                    return self.after_loop(exit_, head, end, loop_outer=outer, out=out, natural=natural)
            out.append(("loop", head, self.region(head, body_end, inner, natural={head})))
            return self.after_loop(exit_, head, end, loop_outer=outer, out=out, natural=natural)
        finally:
            self.active.discard(head)

    # checking the layout

    def verify(self, tree):
        """Disagreements between the laid out tree and the script's jumps, as strings."""
        expect = {}
        for b in self.reach:
            if b in self.merged or b in self.forward:
                continue
            term = self.blocks[b][1]
            if term[0] == "goto":
                expect[b] = ("goto", self.follow(term[1]))
            elif term[0] == "cond":
                expect[b] = ("cond", self.follow(term[2]), self.follow(term[3]))
            elif term[0] == "switch":
                expect[b] = ("switch", {v: self.follow(x) for v, x in term[2].items()},
                             self.follow(term[3]))
            else:
                expect[b] = ("return",)
        got, seen, problems = {}, collections.Counter(), []

        def entry(nodes, i, after, ctx):
            while i < len(nodes):
                node = nodes[i]
                k = node[0]
                if k in ("stmts", "if", "while", "loop", "switch", "return"):
                    return node[1]
                if k == "break":
                    return ctx["brk"]
                if k == "continue":
                    return ctx["cont"]
                if k in ("goto", "inline"):
                    return node[1]
                i += 1
            return after

        def walk(nodes, after, ctx):
            for i, node in enumerate(nodes):
                k = node[0]
                nxt = lambda: entry(nodes, i + 1, after, ctx)  # noqa: E731
                if k == "stmts":
                    seen[node[1]] += 1
                    follower = nodes[i + 1] if i + 1 < len(nodes) else None
                    if follower is None or follower[0] not in ("if", "switch", "return") \
                            or follower[1] != node[1]:
                        got[node[1]] = ("goto", nxt())
                elif k == "if":
                    a = nxt()
                    then_in = entry(node[4], 0, a, ctx)
                    else_in = entry(node[5], 0, a, ctx) if node[5] is not None else a
                    t, f = (then_in, else_in) if node[3] else (else_in, then_in)
                    got[node[1]] = ("cond", t, f)
                    walk(node[4], a, ctx)
                    if node[5] is not None:
                        walk(node[5], a, ctx)
                elif k == "while":
                    a = nxt()
                    seen[node[1]] += 1
                    inner = {"brk": a, "cont": node[1]}
                    body_in = entry(node[4], 0, node[1], inner)
                    got[node[1]] = ("cond", body_in, a) if node[3] else ("cond", a, body_in)
                    walk(node[4], node[1], inner)
                elif k == "loop":
                    a = nxt()
                    walk(node[2], node[1], {"brk": a, "cont": node[1]})
                elif k == "switch":
                    a = nxt()
                    inner = dict(ctx, brk=a)
                    table = {}
                    default_in = a
                    for n, (values, body) in enumerate(node[3]):
                        # falling off a case runs into the next
                        rest = [b for _, b in node[3][n + 1:]]
                        after_case = a
                        for later in reversed(rest):
                            after_case = entry(later, 0, after_case, inner)
                        start = entry(body, 0, after_case, inner)
                        for v in values if values is not None else ["default"]:
                            if v == "default":
                                default_in = start
                            else:
                                table[v] = start
                        walk(body, after_case, inner)
                    if node[5]:
                        for v, x in expect.get(node[1], ("", {}, 0))[1].items():
                            if x == END:
                                table[v] = END
                    got[node[1]] = ("switch", table, default_in)
                elif k == "return":
                    got[node[1]] = ("return",)
                elif k == "goto" and len(node) > 2:
                    # a short returning block written where jumped to
                    got[node[1]] = ("return",)
                elif k == "inline":
                    # a short block written out on one side
                    have = ("goto", nxt())
                    if got.get(node[1], have) != have:
                        problems.append("block %d is written out going to two places" % node[1])
                    got[node[1]] = have

        walk(tree, END, {"brk": None, "cont": None})
        for b, want in expect.items():
            have = got.get(b)
            if have is None:
                problems.append("block %d is not laid out" % b)
            elif have != want:
                problems.append("block %d goes to %s, the script says %s" % (b, have, want))
        for b, n in seen.items():
            if n > 1:
                problems.append("block %d is laid out %d times" % (b, n))
        return problems

    # text

    def text(self):
        self.cut()
        self.graph()
        tree = self.region(0, len(self.code), None, natural={len(self.code), END})
        self.problems = self.verify(tree)
        head = self.head
        params = ["int%d: %s" % (k, self.typename(I, k)) for k in range(head["argInts"])] + \
                 ["string%d: %s" % (k, self.typename(S, k)) for k in range(head["argStrings"])] + \
                 ["long%d: %s" % (k, self.typename(L, k)) for k in range(head["argLongs"])]
        rets = self.ctx.returns.get(self.sid, ())
        shown = [self.rettype(k, kind) for k, kind in enumerate(rets)]
        ret = "void" if not rets else shown[0] if len(rets) == 1 else "[%s]" % ", ".join(shown)
        lines = ["//%s" % self.ctx.script_names.get(self.sid, ""), "function script%d(%s): %s {" % (self.sid, ", ".join(params), ret)]
        if self.used_locals:
            order = sorted(self.used_locals, key=lambda x: ((I, S, L).index(x[0]), x[1]))
            lines.append("    let %s;" % ", ".join(
                "%s%d: %s" % ({I: "int", S: "string", L: "long"}[k], n, self.typename(k, n)) for k, n in order))
        if self.problems:
            # the structured layout failed, so write every jump
            lines.append("    // flat: the structured layout did not match the jumps (%s)" % self.problems[0])
            self.flat(lines)
        else:
            self.render(tree, 1, lines)
        lines.append("}")
        return "\n".join(lines) + "\n"

    def flat(self, lines):
        def to(x):
            x = self.follow(x)
            return "return;" if x == END else "goto label%d;" % x

        for b in self.order:
            if b not in self.reach or b in self.merged or b in self.forward:
                continue
            stmts, term = self.blocks[b]
            lines.append("label%d:" % b)
            lines.extend("    " + s for s in stmts)
            if term[0] == "goto":
                lines.append("    " + to(term[1]))
            elif term[0] == "cond":
                lines.append("    if (%s) %s" % (term[1].text, to(term[2])))
                lines.append("    " + to(term[3]))
            elif term[0] == "switch":
                lines.append("    switch (%s) {" % term[1].text)
                for v, x in sorted(term[2].items()):
                    lines.append("        case %d: %s" % (v, to(x)))
                lines.append("        default: %s" % to(term[3]))
                lines.append("    }")
            else:
                vals = term[1]
                lines.append("    return;" if not vals else "    return %s;" % (
                    vals[0].text if len(vals) == 1 else "[%s]" % ", ".join(v.text for v in vals)))

    def typename(self, kind, n):
        t = self.ctx.votes.get(("local", self.sid, kind, n))
        return ty.name(t) if ty.informative(t) else kind

    def rettype(self, k, kind):
        t = self.ctx.votes.get(("ret", self.sid, k))
        return ty.name(t) if ty.informative(t) else kind

    def render(self, nodes, depth, lines):
        pad = "    " * depth
        for node in nodes:
            k = node[0]
            if k == "stmts":
                if node[1] in self.labels:
                    lines.append("    " * (depth - 1) + "label%d:" % node[1])
                lines.extend(pad + s for s in node[2])
            elif k == "return":
                lines.append(pad + node[2])
            elif k == "if":
                lines.append(pad + "if (%s) {" % node[2].text)
                self.render(node[4], depth + 1, lines)
                if node[5]:
                    lines.append(pad + "} else {")
                    self.render(node[5], depth + 1, lines)
                lines.append(pad + "}")
            elif k == "while":
                if node[1] in self.labels:
                    lines.append("    " * (depth - 1) + "label%d:" % node[1])
                lines.append(pad + "while (%s) {" % node[2].text)
                self.render(node[4], depth + 1, lines)
                lines.append(pad + "}")
            elif k == "loop":
                lines.append(pad + "while (true) {")
                self.render(node[2], depth + 1, lines)
                lines.append(pad + "}")
            elif k == "switch":
                lines.append(pad + "switch (%s) {" % node[2].text)
                for values, body in node[3]:
                    if values is None:
                        lines.append(pad + "    default:")
                    else:
                        shown = [str(v) for v in values if v != "default"]
                        if shown:
                            lines.append(pad + "    case %s:" % ", ".join(shown))
                        if "default" in values:
                            lines.append(pad + "    default:")
                    self.render(body, depth + 2, lines)
                lines.append(pad + "}")
            elif k == "break":
                lines.append(pad + "break;")
            elif k == "continue":
                lines.append(pad + "continue;")
            elif k == "goto" and len(node) > 2:
                lines.extend(pad + s for s in node[2])
            elif k == "inline":
                lines.extend(pad + s for s in node[2])
            elif k == "goto":
                lines.append(pad + ("goto label%d;" % node[1] if node[1] != END else "return;"))


def context(rounds=8):
    ctx = st.Context()
    raw = json.load(open(cs.OPMAP, encoding="utf-8"))["ops"]
    ctx.opmap_kinds = {v["op"]: v["imm"] for v in raw.values()}
    # infer settles every script and votes returns
    st.infer(ctx)
    # names come as hashes in the script index
    known = json.load(open(os.path.join(os.path.dirname(cs.NAMES), "rs-name-hashes.json"), encoding="utf-8"))
    desc = cs.cachefile.descriptor(cs.MAJOR) or {}
    ctx.script_names = {sid: known[str(g["nameHash"])] for sid, g in desc.items()
                        if g.get("nameHash") is not None and str(g["nameHash"]) in known}
    ctx.varbits = [vid for vid, _ in ops.records(ops.load("varbits"))]
    ctx.votes = ty.Votes()
    ty.seed(ctx.votes, cs.OPMAP)
    learn(ctx, rounds)
    # what the ops take and give, kept
    ty.save(ctx.votes)
    return ctx


def learn(ctx, rounds=3, say=None):
    """Run every script's blocks, voting types, and settle; again, as settled types spread."""
    pool = Workers.of(ctx)
    for r in range(rounds):
        for part in pool.run(_vote, ctx.votes.known):
            for slot, counts in part.items():
                ctx.votes.votes[slot].update(counts)
        moved = ctx.votes.settle()
        if say:
            say("types round %d: %d slots moved" % (r + 1, moved))
        if not moved:
            break


class Workers:
    """Processes each holding a copy of ctx, to run a job over every script at once.

    A script's pass reads ctx and writes only its own votes, so the scripts split freely. Each
    job gets the settled types of the moment, which are small; ctx goes over once."""

    def __init__(self, ctx):
        import multiprocessing
        import pickle
        self.chunks = [[] for _ in range(WORKERS)]
        sizes = [0] * WORKERS
        # biggest first, each to the lightest chunk
        for sid in sorted(ctx.code, key=lambda s: -len(ctx.code[s])):
            k = sizes.index(min(sizes))
            self.chunks[k].append(sid)
            sizes[k] += len(ctx.code[sid]) + 50
        self.pool = multiprocessing.get_context("spawn").Pool(
            WORKERS, _worker_start, (pickle.dumps(ctx, protocol=pickle.HIGHEST_PROTOCOL),))

    @staticmethod
    def of(ctx):
        """The pool for this ctx, started once: learning types and writing share it. Pool
        processes end with the program."""
        if id(ctx) not in _POOLS:
            # ctx is kept, so its id cannot repeat
            _POOLS[id(ctx)] = (ctx, Workers(ctx))
        return _POOLS[id(ctx)][1]

    def run(self, job, known, *args):
        return self.pool.map(_worker_job, [(job, sids, known, args) for sids in self.chunks if sids])


WORKERS = min(32, os.cpu_count() or 1)
_POOLS = {}
_WORKER = {}


def _worker_start(blob):
    import pickle
    _WORKER["ctx"] = pickle.loads(blob)
    _WORKER["names"] = cs.names()


def _worker_job(job):
    fn, sids, known, args = job
    ctx = _WORKER["ctx"]
    ctx.votes = ty.Votes()
    ctx.votes.known = known
    return run_deep(fn, ctx, sids, _WORKER["names"], *args)


def _vote(ctx, sids, names):
    for sid in sids:
        try:
            Script(ctx, sid, names).cut()
        except (st.Wrong, st.Stuck, KeyError, IndexError, ValueError):
            continue
    return ctx.votes.votes


def _write(ctx, sids, names, folder):
    decompiled, flat, listed = 0, 0, []
    for sid in sids:
        try:
            text, _, problems = decompile(ctx, sid, names)
            decompiled += 1
            flat += 1 if problems else 0
        except (st.Wrong, st.Stuck, KeyError, IndexError, ValueError, RecursionError) as e:
            text = listing(ctx, sid, names, e)
            listed.append(sid)
        with open(os.path.join(folder, "clientscript-%d.ts" % sid), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    return decompiled, flat, listed


def decompile(ctx, sid, names=None):
    """(text, gotos, layout problems) for one script."""
    names = names if names is not None else cs.names()
    s = Script(ctx, sid, names)
    text = s.text()
    if s.gotos or s.problems:
        # a short block reached from several places
        again = Script(ctx, sid, names)
        again.inline_returns = True
        retry = again.text()
        if not again.problems and (again.gotos < s.gotos or (s.problems and again.gotos <= s.gotos)):
            return retry, again.gotos, again.problems
    return text, s.gotos, s.problems


def check():
    t0 = time.time()
    ctx = context()
    names = cs.names()
    loaded = time.time() - t0
    ok, gotos, with_gotos, wrong_layout = 0, 0, 0, 0
    failed, layout = collections.Counter(), collections.Counter()
    example = {}
    t1 = time.time()
    for sid in sorted(ctx.code):
        try:
            _, g, problems = decompile(ctx, sid, names)
        except (st.Wrong, st.Stuck, KeyError, IndexError, ValueError, RecursionError) as e:
            key = type(e).__name__ + ": " + " ".join("N" if w.isdigit() else w for w in str(e).split()[:6])
            failed[key] += 1
            example.setdefault(key, sid)
            continue
        ok += 1
        gotos += g
        if g:
            with_gotos += 1
            example.setdefault("goto", sid)
        if problems:
            wrong_layout += 1
            key = " ".join("N" if w.strip(",()").lstrip("-").isdigit() else w for w in problems[0].split()[:5])
            layout[key] += 1
            example.setdefault(key, sid)
    print("loaded in %.1fs, decompiled %d of %d in %.1fs" % (loaded, ok, len(ctx.code), time.time() - t1))
    print("gotos %d, in %d scripts, e.g. %s" % (gotos, with_gotos, example.get("goto")))
    print("layout disagrees with the jumps in %d scripts, written flat instead" % wrong_layout)
    for k, n in layout.most_common(8):
        print("  %5d  %-70s eg %d" % (n, k[:70], example[k]))
    for k, n in failed.most_common(8):
        print("  %5d  %-70s eg %d" % (n, k[:70], example[k]))


def write(folder, ctx=None, say=print):
    """Every script into folder as clientscript-N.ts, with clientvars.d.ts and tsconfig.json.

    A script that cannot be decompiled is still written, as its instructions, so every script
    has a file to search. Returns (decompiled, flat, written as instructions: ids)."""
    ctx = ctx or context()
    os.makedirs(folder, exist_ok=True)
    decompiled, flat, listed = 0, 0, []
    for d, f, ids in Workers.of(ctx).run(_write, ctx.votes.known, folder):
        decompiled, flat, listed = decompiled + d, flat + f, listed + ids
    listed.sort()
    with open(os.path.join(folder, "clientvars.d.ts"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(declarations(ctx))
    with open(os.path.join(folder, "tsconfig.json"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write('{\n\t"compilerOptions": {\n\t\t"lib": [],\n\t\t"target": "ESNext"\n\t}\n}\n')
    say("clientscript: %d decompiled (%d of them flat), %d written as instructions"
        % (decompiled, flat, len(listed)))
    return decompiled, flat, listed


def listing(ctx, sid, names, why):
    """A script that would not decompile, as instructions, so it can still be read and searched."""
    head = ctx.heads[sid]
    lines = ["//%s" % ctx.script_names.get(sid, ""),
             "// not decompiled (%s): the instructions follow" % str(why)[:120],
             "function script%d(): void {" % sid]
    for i, (op, imm) in enumerate(ctx.code[sid]):
        name = names.get(op)
        name = name.lower() if name else "op%d" % op
        if isinstance(imm, tuple):
            imm = json.dumps(imm[1]) if imm[0] == S else "%d%s" % (imm[1], "L" if imm[0] == L else "")
        if op == OP["gosub"]:
            name, imm = "script%d" % imm, ""
        elif op in (OP["push_var"], OP["pop_var"]):
            imm = "%s_%d" % (VAR_NAMES.get(imm >> 24, "var%d" % (imm >> 24)), (imm >> 8) & 0xFFFF)
        elif op in (OP["push_varbit"], OP["pop_varbit"]):
            imm = "varbitplayer_%d" % (imm >> 8)
        lines.append("    /* %4d */ %s %s" % (i, name, imm))
    for n, table in enumerate(head["switches"]):
        lines.append("    // switch %d: %s" % (n, ", ".join("%d -> %d" % kv for kv in sorted(table.items()))))
    lines.append("}")
    return "\n".join(lines) + "\n"


def declarations(ctx):
    """Every var a script can name, with its type, as a TypeScript declaration file."""
    lines = []
    by_domain = collections.defaultdict(list)
    for (dom, vid), t in ctx.vars.items():
        by_domain[dom].append((vid, t))
    for dom in sorted(by_domain):
        prefix = VAR_NAMES.get(dom, "var%d" % dom)
        lines.append("// ===== %s =====" % prefix[3:])
        for vid, t in sorted(by_domain[dom]):
            lines.append("declare var %s_%d: %s;" % (prefix, vid, ty.name(t)))
    lines.append("// ===== varbits =====")
    for vid in sorted(ctx.varbits):
        lines.append("declare var varbitplayer_%d: int;" % vid)
    return "\n".join(lines) + "\n"


def run_deep(fn, *args, **kwargs):
    """fn in a thread with room to recurse: long else-if chains nest deep."""
    import threading
    got = {}

    def body():
        try:
            got["value"] = fn(*args, **kwargs)
        except BaseException as e:  # carried back to the caller
            got["error"] = e

    sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))
    threading.stack_size(64 * 1024 * 1024)
    worker = threading.Thread(target=body)
    worker.start()
    worker.join()
    if "error" in got:
        raise got["error"]
    return got["value"]


def _main():
    if sys.argv[1:2] == ["check"]:
        check()
    elif sys.argv[1:2] == ["write"]:
        write(sys.argv[2])
    elif sys.argv[1:] and sys.argv[1].isdigit():
        text, gotos, problems = decompile(context(), int(sys.argv[1]))
        print(text)
        for p in problems:
            print("// layout:", p)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    run_deep(_main)
