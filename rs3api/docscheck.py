"""Does the docs mirror agree with the typings?

The memory files mirroring the client api docs, which nothing had ever checked. Re-fetching and
diffing does not work: only 12 record a source url, and comparing headings measures formatting.
It reported ten missing members on a correct page, because every heading carries a pilcrow.

So compare against the typings, which are local and are what the client exposes.

  documented, not in the typings   the mirror describes something that may not exist
  in the typings, not documented   a real member nobody wrote up

The second is common and mostly harmless. The first costs an afternoon, because a plugin gets
written against it.

Only identifiers the typings know as a type are compared. Parameters, headings and prose are
reported as unmatched rather than as an error.

  rs3_docs_check  the mirror against the typings, per page or whole
"""

import io
import os
import re

# a member reference: backticked, or in a cell
TICKED = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
HEADING = re.compile(r"^#{1,4}\s+(.+?)\s*$", re.M)
# docsfetch.py writes "source: <url>" at the top of each page
SOURCE = re.compile(r"^(?:>\s*)?[Ss]ource:\s*(https?://\S+)", re.M)
# section names, not api members
SECTIONS = {"overview", "examples", "example", "related", "methods", "properties", "constructors",
            "enums", "enumerations", "enumeration", "notes", "usage", "fields", "events",
            "functions", "module", "aliases", "operators", "common", "static", "see", "gotchas",
            "parameters", "returns", "signature", "description", "types", "type"}
_C = {}


def _typings():
    """Every name the typings declare: the types themselves and their members."""
    if _C:
        return _C["names"]
    try:
        from rs3api.api import _all
        decls = _all()
    except Exception:
        decls = []
    names = set()
    for d in decls:
        if d.get("name"):
            names.add(d["name"])
        for m in (d.get("members") or []):
            names.add(m)
    _C["names"] = names
    return names


def _pages():
    from rs3api import docsfetch
    return docsfetch.pages()


def _referenced(text):
    """Identifiers the page names, minus its own section headings."""
    heads = {h.strip().lower().rstrip("s") for h in HEADING.findall(text)}
    out = set()
    for n in TICKED.findall(text):
        if n.lower() in SECTIONS or n.lower().rstrip("s") in heads:
            continue
        if len(n) > 2:
            out.add(n)
    return out


def tool_docs_check(args):
    known = _typings()
    pages = _pages()
    if not pages:
        return {"error": "no docs mirror yet: run rs3api/docsfetch.py"}
    if not known:
        return {"error": "the typings did not parse; rs3_api reads the same files"}

    want = str(args.get("page") or "").strip().lower()
    rows = []
    for name, text in pages.items():
        if want and want not in name:
            continue
        refs = _referenced(text)
        unknown = sorted(r for r in refs if r not in known)
        matched = sorted(r for r in refs if r in known)
        rows.append({"page": name, "referenced": len(refs), "matchedTypings": len(matched),
                     "notInTypings": unknown[:30],
                     "hasSourceUrl": bool(SOURCE.search(text))})

    rows.sort(key=lambda r: -len(r["notInTypings"]))
    nosrc = [r["page"] for r in rows if not r["hasSourceUrl"]]
    if want:
        return {"pages": rows,
                "note": "notInTypings is every identifier the page names that the typings do not "
                        "declare. Most are parameter names or prose, so read it rather than "
                        "treating it as a fault list."}

    total_ref = sum(r["referenced"] for r in rows)
    total_ok = sum(r["matchedTypings"] for r in rows)
    return {"pages": len(rows),
            "identifiersReferenced": total_ref,
            "matchedToTypings": total_ok,
            "matchRate": "%.0f%%" % (100.0 * total_ok / max(total_ref, 1)),
            "withoutSourceUrl": len(nosrc),
            "worst": rows[:12],
            "note": "a low match rate on one page usually means it documents parameters rather "
                    "than members, not that it is wrong. %d of %d pages record no source url, so "
                    "they cannot be re-fetched and checked against the site at all -- that is the "
                    "real gap in the mirror." % (len(nosrc), len(rows))}


TOOLS = [
    {"name": "rs3_docs_check", "fn": tool_docs_check,
     "description": "Check the local docs mirror against the typings: which identifiers a page "
                    "names that the typings do not declare, and how many pages record no source "
                    "url so cannot be re-checked at all. Run before trusting a mirrored page for "
                    "something that matters.",
     "inputSchema": {"type": "object", "properties": {
         "page": {"type": "string", "description": "part of a page name, e.g. npclist"}}}},
]


# what the shared tools ask this module for
from rs3mcp import addons  # noqa: E402
addons.offer("doc_pages", _pages)
