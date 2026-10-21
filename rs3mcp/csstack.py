"""What every clientscript instruction takes off the stacks and puts back.

The VM keeps three stacks, int, long and string. Most ops always move the same values, and the
opcode map carries those. The rest depend on data: a var pushes whatever type the var is, a param
read pushes its param's type, a call takes the callee's arguments and gives back its returns, an
interface hook reads a type string saying what follows it. Those are worked out here from the
cache itself.

simulate() walks every path through a script and fails on a pop from an empty stack, or two paths
reaching one instruction with different stacks. Across all scripts that is the check the effects
are right.

    python rs3mcp/csstack.py verify
"""

import collections
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rs3mcp import clientscript as cs, ops  # noqa: E402

I, L, S = "int", "long", "string"

STRING_TYPES = {36, 50}
LONG_TYPES = {35, 49, 56, 71, 110, 115, 116}

# var domains, off an immediate's top byte
VAR_PAGES = {0: "varplayer", 1: "varnpc", 2: "varclient", 3: "varworld", 4: "varregion",
             5: "varobject", 6: "varclan", 7: "varclansettings", 9: "varplayergroup"}

OP = dict(
    jump=6, branch_not=7, branch_eq=8, branch_lt=9, branch_gt=10, branch_lteq=31, branch_gteq=32,
    ret=21, switch=51, push_int_local=33, pop_int_local=34, push_string_local=35,
    pop_string_local=36, push_long_local=10024, pop_long_local=10237, join=37, gosub=40,
    # varbits pack their id like vars do
    push_var=42, pop_var=43, push_varbit=10982, pop_varbit=10983, push_array=45, pop_array=46,
    define_array=10569, const=9001, discard_int=10004, discard_long=9104, discard_string=10360,
    enum=3408, db_getfield=10094, db_find=10113, db_find_with_count=10100, cc_setparam=10098,
    # UNKNOWN_COMMAND_733: a db find pushing one value
    db_find_733=10257,
)
LONG_BRANCHES = {10153, 10449, 10411, 10321, 10491, 10349}
STEPS = {op for op, name in cs.names().items() if name in ("ADD", "SUB")}
INT_BRANCHES = {OP["branch_not"], OP["branch_eq"], OP["branch_lt"], OP["branch_gt"],
                OP["branch_lteq"], OP["branch_gteq"]}
# a param read: the thing, then the id
GETPARAM = {4500, 4208, 10262, 10076, 10049, 10192}
CC_PARAM = 10049


def prim(t):
    return S if t in STRING_TYPES else L if t in LONG_TYPES else I


class Context:
    """The data dynamic ops depend on, read from the cache through our specs."""

    def __init__(self, opmap_path=cs.OPMAP, skip_unread=False):
        """skip_unread: a script that doesn't read under this map is left out (self.unread) rather
        than raising. Calibration's trial maps leave some unread; a finished map must read all."""
        raw = json.load(io.open(opmap_path, encoding="utf-8"))["ops"]
        self.opmap = {int(k): (v["op"], v["imm"]) for k, v in raw.items()}
        self.fixed = {v["op"]: (v["in"], v["out"]) for v in raw.values()
                      if v["in"] is not None and not v.get("inferred")}
        # effects infer() worked out, kept apart
        self.guess = {v["op"]: (v["in"], v["out"]) for v in raw.values() if v.get("inferred")}
        # every hook setter has its family's shape
        names = cs.names()
        for v in raw.values():
            name = names.get(v["op"], "")
            if v["op"] not in self.fixed and name.startswith(("IF_SETON", "CC_SETON")):
                self.fixed[v["op"]] = ([I, "vararg", I] if name.startswith("IF_") else [I, "vararg"], [])
        self.vars = {}
        for dom, page in VAR_PAGES.items():
            spec = ops.load(page)
            for cid, data in ops.records(spec):
                self.vars[(dom, cid)] = ops.decode(spec, data)[0].get("type", 0)
        spec = ops.load("params")
        self.params = {}
        for cid, data in ops.records(spec):
            self.params[cid] = (ops.decode(spec, data)[0].get("type") or {}).get("varType", 0)
        spec = ops.load("dbtables")
        self.columns = {}
        for tid, data in ops.records(spec):
            body = ops.decode(spec, data)[0].get("columns") or {}
            for col in body.get("columns") or []:
                self.columns[(tid, col["id"])] = col["types"]
        self.heads = {}
        self.code = {}
        self.unread = []
        for sid, data in cs.scripts():
            try:
                head = cs.read(data, self.opmap)
            except cs.Bad:
                if not skip_unread:
                    raise
                self.unread.append(sid)
                continue
            self.code[sid] = head.pop("code")
            self.heads[sid] = head
        # what each script leaves for its caller
        self.returns = {}


class Stuck(Exception):
    """An effect cannot be worked out yet, usually a call to a script whose returns are unknown."""


class Wrong(Exception):
    """The stacks cannot be what the code says they are."""


def effect(ctx, op, imm, stack):
    """(values popped as [(stack, const)], types pushed) for one instruction.

    stack is the list of (stack, const) entries below it, so an op can read a constant that
    was pushed before it, as enum does with its types."""
    def top(kind, n=0):
        seen = [e for e in stack if e[0] == kind]
        if len(seen) <= n:
            raise Wrong("%s reads past the %s stack" % (op, kind))
        return seen[-1 - n][1]

    if op == OP["const"]:
        return [], [imm[0]]
    if op in (OP["push_int_local"], OP["push_varbit"]):
        return [], [I]
    if op in (OP["pop_int_local"], OP["pop_varbit"], OP["discard_int"], OP["switch"],
              OP["define_array"]):
        return [I], []
    if op == OP["push_string_local"]:
        return [], [S]
    if op in (OP["pop_string_local"], OP["discard_string"]):
        return [S], []
    if op == OP["push_long_local"]:
        return [], [L]
    if op in (OP["pop_long_local"], OP["discard_long"]):
        return [L], []
    if op == OP["jump"]:
        return [], []
    if op in INT_BRANCHES:
        return [I, I], []
    if op in LONG_BRANCHES:
        return [L, L], []
    if op == OP["join"]:
        return [S] * imm, [S]
    if op == OP["push_array"]:
        return [I], [I]
    if op == OP["pop_array"]:
        return [I, I], []
    if op in (OP["push_var"], OP["pop_var"]):
        t = ctx.vars.get((imm >> 24, (imm >> 8) & 0xFFFF))
        if t is None:
            raise Wrong("var %d:%d is not in the var configs" % (imm >> 24, (imm >> 8) & 0xFFFF))
        return ([], [prim(t)]) if op == OP["push_var"] else ([prim(t)], [])
    if op == OP["gosub"]:
        head = ctx.heads.get(imm)
        if head is None and imm in ctx.unread:
            # exists, but doesn't read under this trial map: unknown, not wrong
            raise Stuck("call to unread script %d" % imm)
        if head is None:
            raise Wrong("call to script %d, which does not exist" % imm)
        if imm not in ctx.returns:
            raise Stuck(imm)
        return ([I] * head["argInts"] + [S] * head["argStrings"] + [L] * head["argLongs"],
                list(ctx.returns[imm]))
    if op in GETPARAM:
        pid = top(I)
        if pid is None:
            raise Stuck("param not constant")
        # a CC op works on the current component
        return [I] if op == CC_PARAM else [I, I], [prim(ctx.params.get(pid, 0))]
    if op == OP["enum"]:
        vtype = top(I, 2)
        if vtype is None:
            raise Stuck("enum value type not constant")
        return [I, I, I, I], [prim(vtype)]
    if op == OP["db_getfield"]:
        col = top(I, 1)
        if col is None:
            raise Stuck("db column not constant")
        types = ctx.columns.get((col >> 12, (col >> 4) & 0xFF))
        if types is None:
            raise Wrong("db column %d names no column" % col)
        # the low nibble picks one tuple value
        pick = col & 0xF
        if pick:
            types = types[pick - 1:pick]
        return [I, I, I], [prim(t) for t in types]
    if op in (OP["db_find"], OP["db_find_with_count"], OP["db_find_733"]):
        # the column, a value of its type, ints
        pushes = [] if op == OP["db_find"] else [I]
        tail = [I, I] if op == OP["db_find_733"] else [I]
        for kind in (S, I, L):
            under = len(tail) + (kind == I)
            col = top(I, under) if len([e for e in stack if e[0] == I]) > under else None
            types = ctx.columns.get((col >> 12, (col >> 4) & 0xFF)) if col is not None else None
            if types and prim(types[col & 0xF] if (col & 0xF) < len(types) else types[0]) == kind:
                return [I, kind] + tail, pushes
        raise Stuck("db find column not constant")
    got = ctx.fixed.get(op) or ctx.guess.get(op)
    if got is None:
        raise Stuck("no effect for op %d" % op)
    pops, pushes = got
    if "vararg" in pops:
        at = pops.index("vararg")
        after = pops[at + 1:]
        return pops[:at] + hook(top, after.count(I)) + after, list(pushes)
    return list(pops), list(pushes)


def _hooklike(ctx, stack):
    """What an unknown op would pop as a hook setter, if the stack in front looks like one.

    That is a signature string on top, or under one int for the IF form, with the args it names
    below and a script id under those. Ints left under the script id are the op's own, pushed
    first, as in `50, script, x, y, "ii"`. Returns (shape, pops) or None."""
    def top(kind, n=0):
        seen = [e for e in stack if e[0] == kind]
        if len(seen) <= n:
            raise Wrong("short")
        return seen[-1 - n][1]

    for above in (0, 1):
        want = [S] if not above else [S, I]
        if [k for k, _ in stack[-len(want):]] != want:
            continue
        sig = top(S)
        if not isinstance(sig, str) or not sig.isalpha() and sig:
            return None
        try:
            args = hook(top, above)
            script = top(I, above + args.count(I))
        except (Wrong, Stuck):
            return None
        if script in ctx.code:
            first = len([k for k, _ in stack if k == I]) - 1 - above - args.count(I)
            return [I] * first + [I, "vararg"] + [I] * above, [I] * (first + 1) + args + [I] * above
    return None


def hook(top, above=0):
    """What an interface hook takes, from the type string pushed just before it.

    The string names one argument per character, `s` a string and anything else an int. A
    trailing Y means a list of trigger ids follows, counted by an int. The script id under the
    arguments is the op's own int, not part of this, and above ints sit over the count."""
    sig = top(S)
    if sig is None:
        raise Stuck("hook signature not constant")
    pops = []
    triggers = sig.endswith("Y")
    for ch in sig[:-1] if triggers else sig:
        pops.append(sigkind(ch))
    if triggers:
        count = top(I, above)
        if count is None:
            raise Stuck("trigger count not constant")
        if count > 64:
            raise Wrong("a hook with %d triggers" % count)
        pops += [I] * count + [I]
    return pops + [S]


def sigkind(ch):
    """The stack a hook signature character takes its argument from."""
    return S if ch == "s" else L if ch == "l" else I


def apply(stack, pops, pushes, op):
    for kind in reversed(pops):
        for j in range(len(stack) - 1, -1, -1):
            if stack[j][0] == kind:
                del stack[j]
                break
        else:
            raise Wrong("op %d pops an empty %s stack" % (op, kind))
    return stack


def targets(code, i):
    """Where control can go after instruction i."""
    op, imm = code[i]
    if op == OP["ret"]:
        return []
    if op == OP["jump"]:
        return [i + 1 + imm]
    if op in INT_BRANCHES or op in LONG_BRANCHES:
        return [i + 1, i + 1 + imm]
    return [i + 1]


def simulate(ctx, sid, samples=None):
    """The stack at every instruction, and the types the script returns.

    samples, when given, collects the stack depths in front of every op infer() is guessing."""
    code, head = ctx.code[sid], ctx.heads[sid]
    at = {0: []}
    work = [0]
    returns = None
    while work:
        i = work.pop()
        if i < 0 or i >= len(code):
            raise Wrong("control reaches instruction %d of %d" % (i, len(code)))
        stack = list(at[i])
        op, imm = code[i]
        if op == OP["ret"]:
            got = tuple(e[0] for e in stack)
            if returns is not None and sorted(returns) != sorted(got):
                raise Wrong("returns %s in one place and %s in another" % (returns, got))
            returns = got
            continue
        if samples is not None and op in ctx.guess:
            kinds = [e[0] for e in stack]
            samples[op].append((kinds.count(I), kinds.count(L), kinds.count(S)))
        try:
            pops, pushes = effect(ctx, op, imm, stack)
            apply(stack, pops, pushes, op)
        except Wrong as e:
            raise Wrong("%s, at %d" % (e, i))
        for kind in pushes:
            stack.append((kind, imm[1] if op == OP["const"] else None))
        nexts = targets(code, i)
        if op == OP["switch"]:
            nexts = nexts + [i + 1 + j for j in head["switches"][imm].values()]
        for n in nexts:
            shape = [e[0] for e in stack]
            if n in at:
                if [e[0] for e in at[n]] != shape:
                    raise Wrong("instruction %d is reached with two different stacks" % n)
                continue
            at[n] = list(stack)
            work.append(n)
    return at, returns or ()



def settle(ctx, samples=None, dirty=None):
    """Simulate every script once, callees before callers.

    A script that fails leaves no returns, so its callers are stuck rather than wrong on a guess.
    Scripts in a call cycle go last, and stay stuck unless another path settles them.
    Returns ({script: Wrong}, {script: Stuck})."""
    callees = {sid: {imm for op, imm in code if op == OP["gosub"] and imm != sid and imm in ctx.code}
               for sid, code in ctx.code.items()}
    callers = collections.defaultdict(set)
    for sid, deps in callees.items():
        for d in deps:
            callers[d].add(sid)
    left = {sid: len(deps) for sid, deps in callees.items()}
    queue = collections.deque(sorted(sid for sid, n in left.items() if n == 0))
    wrong, stuck = {}, {}
    done = set()

    def run(sid):
        # depths only count from a script that simulates
        seen = collections.defaultdict(list) if samples is not None else None
        try:
            ctx.returns[sid] = simulate(ctx, sid, seen)[1]
            stuck.pop(sid, None)
            into = samples
        except Stuck as e:
            stuck[sid] = e
            into = dirty
        except Wrong as e:
            wrong[sid] = e
            into = dirty
        if seen and into is not None:
            for op, got in seen.items():
                into[op].extend(got)

    while queue:
        sid = queue.popleft()
        done.add(sid)
        run(sid)
        for c in callers.get(sid, ()):
            left[c] -= 1
            if left[c] == 0:
                queue.append(c)
    # a call cycle: guess nothing, then take returns
    cycle = sorted(set(ctx.code) - done)
    for sid in cycle:
        ctx.returns.setdefault(sid, ())
    for _ in range(10):
        moved = False
        for sid in cycle:
            before = ctx.returns.get(sid)
            run(sid)
            if sid in wrong or sid in stuck:
                continue
            moved |= ctx.returns[sid] != before
        if not moved:
            break
        for sid in cycle:
            wrong.pop(sid, None)
            stuck.pop(sid, None)
    for sid in cycle:
        if sid in wrong or sid in stuck:
            ctx.returns.pop(sid, None)
    ctx.settled = (wrong, stuck)
    return wrong, stuck


def settled(ctx):
    """settle(ctx), unless nothing has changed since the last one. Whatever changes an effect or a
    return without settling again sets ctx.settled to None."""
    return getattr(ctx, "settled", None) or settle(ctx)


def _key(e):
    text = str(e).split(", at ")[0].split(" in another")[0]
    return " ".join("N" if w.isdigit() else w for w in text.split())


def verify(ctx=None):
    ctx = ctx or Context()
    wrong, stuck = settle(ctx)
    why = collections.Counter(_key(e) for e in wrong.values())
    example = {}
    for sid, e in sorted(wrong.items()):
        example.setdefault(_key(e), sid)
    on = collections.Counter("call" if str(e).isdigit() else _key(e) for e in stuck.values())
    return {"scripts": len(ctx.code), "simulated": len(ctx.code) - len(wrong) - len(stuck),
            "wrong": [{"why": k, "scripts": n, "example": example[k]} for k, n in why.most_common(20)],
            "stuck": len(stuck), "stuckOn": on.most_common(12)}


SINKS = {OP["pop_int_local"], OP["pop_string_local"], OP["pop_long_local"], OP["pop_var"],
         OP["pop_varbit"], OP["discard_int"], OP["discard_string"], OP["discard_long"],
         OP["switch"], OP["ret"], OP["jump"], OP["pop_array"], OP["define_array"]} \
    | INT_BRANCHES | LONG_BRANCHES
ASSIGNS = {OP["pop_int_local"], OP["pop_string_local"], OP["pop_long_local"], OP["pop_var"],
           OP["pop_varbit"], OP["discard_int"], OP["discard_string"], OP["discard_long"]}


def _vec(kinds):
    return (kinds.count(I), kinds.count(L), kinds.count(S))


def infer(ctx, rounds=8, say=None):
    """Effects for ops the map has none for, and returns for scripts simulation cannot settle.

    The compiler leaves the stacks empty between statements. A statement ends after an op that
    leaves nothing behind, and at every jump target, so the ops of one statement come to nothing
    on each stack. A statement holding one unknown gives what that unknown adds, and the depth
    in front of it caps what it pops: where it is not nested inside a bigger expression that
    depth is exactly its pops, so the least seen is the count.

    Unknowns are ops, and what a script returns. Every statement votes and the majority is
    taken, round after round as solved unknowns free up statements that held two."""
    trim(ctx, say)
    last = None
    for _ in range(rounds):
        nets = collections.defaultdict(collections.Counter)
        depths = collections.defaultdict(list)
        for sid, code in ctx.code.items():
            _statements(ctx, sid, code, nets, depths)
        solved = 0
        for key, votes in nets.items():
            got = votes.most_common(1)[0][0]
            if key[0] == "ret":
                if key[1] in ctx.returns or min(got) < 0:
                    continue
                ctx.returns[key[1]] = tuple([I] * got[0] + [L] * got[1] + [S] * got[2])
            else:
                if key[1] in ctx.guess:
                    continue
                seen = depths.get(key) or [(0, 0, 0)]
                pops = tuple(max(min(d[k] for d in seen), -got[k]) for k in range(3))
                pushes = tuple(p + n for p, n in zip(pops, got))
                ctx.guess[key[1]] = ([I] * pops[0] + [L] * pops[1] + [S] * pops[2],
                                     [I] * pushes[0] + [L] * pushes[1] + [S] * pushes[2])
            solved += 1
        if not solved:
            break
        wrong, stuck = settle(ctx)
        if say:
            say("round: %d solved, %d wrong, %d stuck" % (solved, len(wrong), len(stuck)))
        # a cycled script can solve again and fail
        if last == (set(wrong), set(stuck)):
            break
        last = (set(wrong), set(stuck))
    guess_rest(ctx, say)
    trim(ctx, say)
    return dict(ctx.guess)


def trim(ctx, say=None):
    """Drop a value a seeded effect carries through, where a script proves the op never had it.

    A seeded map can have an op take and give back one more value of a kind than it really does.
    Nested inside a bigger expression the two look the same; at the start of a statement the op
    pops a stack that is empty. The trim stays only if no script goes wrong that did not before."""
    wrong, _ = settled(ctx)
    tried = set()
    while True:
        for sid, e in sorted(wrong.items()):
            m = re.match(r"op (\d+) pops an empty (\w+) stack", str(e))
            op = int(m.group(1)) if m else None
            got = ctx.fixed.get(op) or ctx.guess.get(op)
            kind = m.group(2) if m else None
            if op in tried or not got or "vararg" in got[0] or kind not in got[0] or kind not in got[1]:
                continue
            tried.add(op)
            pops, pushes = list(got[0]), list(got[1])
            pops.remove(kind)
            pushes.remove(kind)
            fixed = ctx.fixed.pop(op, None)
            ctx.guess[op] = (pops, pushes)
            # the script that asked has to stop failing
            try:
                simulate(ctx, sid)
                still = False
            except (Wrong, Stuck) as again:
                still = str(again).startswith("op %d pops an empty" % op)
            if still:
                ctx.guess.pop(op)
                if fixed:
                    ctx.fixed[op] = fixed
                else:
                    ctx.guess[op] = got
                continue
            # settling only changes returns, so putting back undoes
            before = (dict(ctx.returns), settled(ctx))
            now, _ = settle(ctx)
            if len(now) < len(wrong) and not set(now) - set(wrong):
                if say:
                    say("trimmed op %d to %s -> %s, wrong %d -> %d" % (op, pops, pushes, len(wrong), len(now)))
                wrong = now
                break
            ctx.guess.pop(op)
            if fixed:
                ctx.fixed[op] = fixed
            else:
                ctx.guess[op] = got
            ctx.returns.clear()
            ctx.returns.update(before[0])
            ctx.settled = before[1]
        else:
            return


def guess_rest(ctx, say=None):
    """Effects for the ops that only ever sit next to other unknowns, as a last resort.

    Each is a setter taking everything in front of it, or a getter pushing one int, with or
    without args. A guess goes if a script it is in goes wrong with it."""
    names = cs.names()
    votes = collections.defaultdict(collections.Counter)
    for sid, code in ctx.code.items():
        _greedy(ctx, sid, code, names, votes)
    got = {op: v.most_common(1)[0][0] for op, v in votes.items() if op not in ctx.guess}
    was, stuck = settled(ctx)
    wrong, before = was, len(was) + len(stuck)
    for op, (pops, pushes) in got.items():
        ctx.guess[op] = (list(pops), list(pushes))
    # a guess that went wrong with them goes
    while got:
        wrong, stuck = settle(ctx)
        bad = {op for sid in set(wrong) - set(was) for op, _ in ctx.code[sid]} & set(got)
        if not bad:
            break
        for op in bad:
            ctx.guess.pop(op)
            got.pop(op)
        ctx.settled = None
    if say:
        say("last resort: %d ops, failing %d -> %d" % (len(got), before, len(wrong) + len(stuck)))
    return got


GETTER = re.compile(r"(^|_)(GET|IS|HAS|FIND)")


def _greedy(ctx, sid, code, names, votes):
    """guess_rest's vote for every unknown op in one script.

    Statements are cut only where known ops end them. In each, every unknown is tried as a
    setter, a getter of its args, and a bare getter, the shape its name suggests first, and the
    first mix found that leaves the stacks as the statement needs them votes."""
    handled = set(OP.values()) | GETPARAM | INT_BRANCHES | LONG_BRANCHES
    for a, b in _segments(ctx, sid, code):
        unknown = {i for i in range(a, b + 1) if code[i][0] not in handled
                   and code[i][0] not in ctx.fixed and code[i][0] not in ctx.guess}
        if unknown:
            cast = _replay(ctx, sid, code, a, b, unknown, names, [], 0, [], [300])
            for op, e in cast or ():
                votes[op][e] += 1


def _replay(ctx, sid, code, i, b, unknown, names, stack, mark, cast, budget):
    """The effects the unknowns from instruction i on take, trying shapes depth first, or None.

    budget is how many shapes may still be tried, shared across the whole search."""
    for i in range(i, b + 1):
        op, imm = code[i]
        if op == OP["ret"]:
            # an unsettled script most likely returns nothing
            want = _vec(ctx.returns.get(sid, ()))
            return cast if want == _vec([k for k, _ in stack]) else None
        if i in unknown:
            hooked = _hooklike(ctx, stack)
            kinds = [k for k, _ in stack]
            if hooked:
                tries = [(hooked[1], [], (tuple(hooked[0]), ()))]
            else:
                getter = GETTER.search(names.get(op) or "")
                tries = []
                for shape in ("bare", "args", "void") if getter else ("void", "args", "bare"):
                    pops = {"void": kinds, "args": kinds[min(mark, len(kinds)):], "bare": []}[shape]
                    pushes = [] if shape == "void" else [I]
                    tries.append((pops, pushes, (tuple(sorted(pops, key=(I, L, S).index)), tuple(pushes))))
            for pops, pushes, e in tries:
                if budget[0] <= 0:
                    return None
                budget[0] -= 1
                after = list(stack)
                for kind in pops:
                    _drop(after, kind)
                after.extend((kind, None) for kind in pushes)
                got = _replay(ctx, sid, code, i + 1, b, unknown, names, after, len(after),
                              cast + [(op, e)], budget)
                if got is not None:
                    return got
            return None
        try:
            pops, pushes = effect(ctx, op, imm, stack)
        except Stuck:
            # the stacks were empty, so a statement ended
            return cast if not stack and cast else None
        except Wrong:
            return None
        for kind in pops:
            if not any(k == kind for k, _ in stack):
                return None
            _drop(stack, kind)
        for kind in pushes:
            stack.append((kind, imm[1] if op == OP["const"] else None))
    return cast if not stack else None


def _postfix(code, i):
    """Whether instruction i stores `x++` or `x--` used as a value, which leaves the old x on
    the stack, so the statement goes on past the store."""
    if i < 4 or code[i][0] != OP["pop_int_local"]:
        return False
    x = code[i][1]
    return code[i - 1][0] in STEPS and code[i - 2] == (OP["const"], (I, 1)) \
        and code[i - 3] == (OP["push_int_local"], x) and code[i - 4] == (OP["push_int_local"], x)


def _segments(ctx, sid, code):
    """(first, last) of every statement, cut where a known op ends one."""
    starts = _starts(ctx, sid, code)
    out, a = [], 0
    for i, (op, imm) in enumerate(code):
        if i in starts and i > a:
            out.append((a, i - 1))
            a = i
        if op in ASSIGNS and i + 1 < len(code) and code[i + 1][0] in ASSIGNS or _postfix(code, i):
            continue
        got = ctx.fixed.get(op) or ctx.guess.get(op)
        if op in SINKS or got is not None and not got[1] \
                or op == OP["gosub"] and ctx.returns.get(imm) == ():
            out.append((a, i))
            a = i + 1
    return out


def _starts(ctx, sid, code):
    head = ctx.heads[sid]
    starts = set()
    for i, (op, imm) in enumerate(code):
        if op == OP["jump"] or op in INT_BRANCHES or op in LONG_BRANCHES:
            starts.add(i + 1 + imm)
        if op == OP["switch"]:
            starts.update(i + 1 + j for j in head["switches"][imm].values())
    return starts


def _statements(ctx, sid, code, nets, depths):
    """Vote on the unknowns in every statement of one script."""
    starts = _starts(ctx, sid, code)
    stack, total, unknown, clean, first = [], [0, 0, 0], [], True, None
    for i, (op, imm) in enumerate(code):
        if i in starts:
            stack, total, unknown, clean = [], [0, 0, 0], [], True
        if op == OP["ret"]:
            n = len(unknown)
            if clean and not unknown and sid not in ctx.returns:
                nets[("ret", sid)][tuple(total)] += 1
            # the returns are known, so the unknown balances
            elif clean and n and len(set(unknown)) == 1 and sid in ctx.returns \
                    and unknown[0] != ("ret", sid):
                left = [r - t for r, t in zip(_vec(ctx.returns[sid]), total)]
                if all(x % n == 0 for x in left):
                    nets[unknown[0]][tuple(x // n for x in left)] += 1
            stack, total, unknown, clean = [], [0, 0, 0], [], True
            continue
        key = None
        try:
            pops, pushes = effect(ctx, op, imm, stack)
        except Stuck as e:
            if op == OP["gosub"] and isinstance(e.args[0], int):
                h = ctx.heads[imm]
                pops, pushes = [I] * h["argInts"] + [S] * h["argStrings"] + [L] * h["argLongs"], []
                key = ("ret", imm)
            elif str(e).startswith("no effect") and not _hooklike(ctx, stack):
                pops, pushes, key = [], [], ("op", op)
            else:
                # a hook setter's pops hang on its signature
                pops, pushes, clean = [], [], False
        except Wrong:
            pops, pushes, clean = [], [], False
        if key is not None:
            if not unknown:
                first = tuple(total) if key[0] == "op" and clean else None
                if first is not None:
                    depths[key].append(first)
            unknown.append(key)
        for kind in pops:
            total[(I, L, S).index(kind)] -= 1
            _drop(stack, kind)
        for kind in pushes:
            total[(I, L, S).index(kind)] += 1
            stack.append((kind, imm[1] if op == OP["const"] else None))
        # several locals stored in a row, ending last
        if op in ASSIGNS and i + 1 < len(code) and code[i + 1][0] in ASSIGNS or _postfix(code, i):
            continue
        if op in SINKS or (key is None and not pushes):
            # one unknown, maybe several times over
            n = len(unknown)
            if clean and n and len(set(unknown)) == 1 and all(x % n == 0 for x in total):
                net = tuple(-x // n for x in total)
                # an op cannot take more than preceded it
                if first is None or all(-x <= d for x, d in zip(net, first)):
                    nets[unknown[0]][net] += 1
            stack, total, unknown, clean = [], [0, 0, 0], [], True


def _drop(stack, kind):
    for j in range(len(stack) - 1, -1, -1):
        if stack[j][0] == kind:
            stack.pop(j)
            return


def save(ctx, path=cs.OPMAP):
    """Write the inferred effects into the opcode map, marked as inferred."""
    d = json.load(io.open(path, encoding="utf-8"))
    for entry in d["ops"].values():
        if entry["op"] in ctx.guess:
            entry["in"], entry["out"] = ctx.guess[entry["op"]]
            entry["inferred"] = True
    json.dump(d, io.open(path, "w", encoding="utf-8"), indent=0, sort_keys=True)

def trace(sid, around=None):
    """Instructions with the stack before each, up to where simulation stops."""
    ctx = Context()
    settle(ctx)
    names = cs.names()
    code = ctx.code[sid]
    try:
        at, ret = simulate(ctx, sid)
        failed = None
    except (Wrong, Stuck) as e:
        failed = str(e)
        at = _partial(ctx, sid)
    lo, hi = (0, len(code)) if around is None else (max(0, around - 12), around + 4)
    for i in range(lo, min(hi, len(code))):
        op, imm = code[i]
        st = at.get(i)
        shape = " ".join("%s%s" % (e[0][0], "" if e[1] is None else "=%r" % (e[1],)) for e in st) \
            if st is not None else "unreached"
        print("%5d  %-30s %-14s | %s" % (i, names.get(op, "op%d" % op), str(imm)[:14], shape[-70:]))
    print("stopped:", failed) if failed else print("returns", ret)


def _partial(ctx, sid):
    """The stacks reached before a failure, by walking in order and stopping at the first error."""
    code, at, stack = ctx.code[sid], {}, []
    for i, (op, imm) in enumerate(code):
        at[i] = list(stack)
        try:
            pops, pushes = effect(ctx, op, imm, stack)
            apply(stack, pops, pushes, op)
        except (Wrong, Stuck):
            break
        stack.extend((k, imm[1] if op == OP["const"] else None) for k in pushes)
    return at


if __name__ == "__main__":
    if sys.argv[1:2] == ["verify"]:
        got = verify()
        print("scripts %(scripts)d, simulated %(simulated)d, stuck %(stuck)d" % got)
        print("stuck on", got["stuckOn"])
        for w in got["wrong"]:
            print("  %5d  %-100s eg %d" % (w["scripts"], w["why"][:100], w["example"]))
    elif sys.argv[1:2] == ["infer"]:
        ctx = Context()
        got = infer(ctx, say=print)
        if "--save" in sys.argv:
            save(ctx)
        names = cs.names()
        for op in sorted(got):
            print("%6d %-36s in %-30s out %s" % (op, names.get(op, "?"), got[op][0], got[op][1]))
        print(json.dumps(verify(ctx)["stuckOn"]))
    elif sys.argv[1:2] == ["trace"]:
        trace(int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else None)
    else:
        raise SystemExit(__doc__)