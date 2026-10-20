"""Script types: what a clientscript value is, beyond int, long or string.

The client knows around 130 of them: an obj, a component, a coordinate, a colour. Nothing in a
script says which a value is, so it is learned from where values come from and where they go. A
var or a param carries its type in the cache; an op takes and gives particular types; a local
takes the type of what is stored in it. Every place a typed value meets an untyped one is a vote,
and each slot takes the majority, over all scripts at once, round after round.

The learned op signatures are kept in opcodes/clientscript-types.json keyed by op id, so the next
update starts from them.
"""

import collections
import io
import json
import os


STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "opcodes", "clientscript-types.json")

NAMES = {
    0: "int", 1: "boolean", 3: "quest", 4: "questhelp", 5: "cursor", 6: "seq", 7: "colour",
    8: "loc_shape", 9: "component", 10: "idkit", 11: "midi", 12: "npc_mode", 13: "namedobj",
    14: "synth", 16: "area", 17: "stat", 18: "npc_stat", 19: "writeinv", 20: "mesh",
    21: "maparea", 22: "coordgrid", 23: "graphic", 24: "chatphrase", 25: "fontmetrics",
    26: "enum", 28: "jingle", 29: "chatcat", 30: "loc", 31: "model", 32: "npc", 33: "obj",
    34: "player_uid", 36: "string", 37: "spotanim", 38: "npc_uid", 39: "inv", 40: "texture",
    41: "category", 42: "char", 43: "laser", 44: "bas", 46: "collision_geometry",
    47: "physics_model", 48: "physics_control_modifier", 49: "clanhash", 50: "coordfine",
    51: "cutscene", 53: "itemcode", 55: "mapsceneicon", 56: "clanforumqfc", 57: "vorbis",
    58: "verify_object", 59: "mapelement", 60: "categorytype", 61: "social_network",
    62: "hitmark", 63: "package", 64: "particle_effector", 66: "particle_emitter",
    67: "plogtype", 68: "unsigned_int", 69: "skybox", 70: "skydecor", 71: "hash64",
    72: "inputtype", 73: "struct", 74: "dbrow", 89: "gwc_platform", 94: "bug_template",
    95: "billing_auth_flag", 96: "account_feature_flag", 97: "interface",
    98: "toplevelinterface", 99: "overlayinterface", 100: "clientinterface", 101: "movespeed",
    102: "material", 103: "seqgroup", 104: "temp_hiscore", 108: "audiogroup",
    109: "audiomixbuss", 110: "long", 111: "crm_channel", 112: "http_image", 114: "poll",
    117: "pointlight", 118: "player_group", 125: "client_type", 131: "achievement",
    133: "stylesheet", 209: "var_reference",
}

# types naming only the stack a value uses
PLAIN = {0, 36, 110, 501, 502, 503}
# placeholders for a type nobody could tell
UNKNOWN = {501, 502, 503}


def name(t):
    return NAMES.get(t, "type_%d" % t)


def informative(t):
    return t is not None and t not in PLAIN


def literal(value, t):
    """How a constant reads once its type is known, or None to leave it a number."""
    if not informative(t) or not isinstance(value, int):
        return None
    if t == 1:
        return {0: "false", 1: "true"}.get(value)
    if value == -1:
        return "null"
    if t == 9:
        return "comp(%d, %d)" % (value >> 16, value & 0xFFFF)
    if t == 7:
        return "0x%06X" % value
    if t == 22:
        return "coord(%d, %d, %d)" % (value >> 28, (value >> 14) & 0x3FFF, value & 0x3FFF)
    if t in (17, 68):
        return None if t == 68 else "%d as stat" % value
    return "%d as %s" % (value, name(t))


class Votes:
    """Evidence for each slot, and what each slot settled on last round."""

    def __init__(self):
        self.votes = collections.defaultdict(collections.Counter)
        self.known = {}

    def vote(self, slot, t):
        # a plain int is a vote too
        if slot is not None and t is not None and t not in UNKNOWN:
            self.votes[slot][t] += 1

    def get(self, slot):
        return self.known.get(slot)

    def settle(self):
        """Take the majority for every slot with votes. Returns how many changed."""
        changed = 0
        for slot, counts in self.votes.items():
            best = min(counts, key=lambda t: (-counts[t], t))
            if self.known.get(slot) != best:
                self.known[slot] = best
                changed += 1
        self.votes = collections.defaultdict(collections.Counter)
        return changed


def seed(votes, opmap_path=None):
    """Op signatures to start from: our store if there is one, else what the opmap carries."""
    if os.path.exists(STORE):
        for key, t in json.load(io.open(STORE, encoding="utf-8")).items():
            op, side, stack, i = key.split(":")
            votes.known[("op", int(op), side, stack, int(i))] = t
        return
    if opmap_path and os.path.exists(opmap_path):
        for entry in json.load(io.open(opmap_path, encoding="utf-8"))["ops"].values():
            for side in ("inT", "outT"):
                for stack, types in (entry.get(side) or {}).items():
                    for i, t in enumerate(types):
                        if informative(t):
                            votes.known[("op", entry["op"], side, stack, i)] = t


def save(votes):
    ops = {"%d:%s:%s:%d" % k[1:]: t for k, t in votes.known.items() if k[0] == "op"}
    json.dump(ops, io.open(STORE, "w", encoding="utf-8"), indent=0, sort_keys=True)
