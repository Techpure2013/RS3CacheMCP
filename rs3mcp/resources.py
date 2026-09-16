"""Readable context, addressed by uri rather than reached through a tool call.

Something has to decide to call a tool. Resources are attached by the client instead.

Two kinds: a short static list, and templates for the spaces too big to enumerate. A workspace's
add-on can offer more of both through rs3mcp.addons, under "resources".
"""

import io
import os

from . import addons

MD = "text/markdown"


def tool_text(name, args=None):
    """One tool's answer as text, for the resources that are a tool's output in another shape."""
    import json
    from .common import registry
    t = registry().get(name)
    if t is None:
        return None
    try:
        return json.dumps(t["fn"](args or {}), indent=1, default=str)
    except Exception as e:
        return "this resource could not be built: %s" % e


def read_file(path, mime):
    if not os.path.exists(path):
        return None
    return (io.open(path, encoding="utf-8", errors="replace").read(), mime)


def _instructions():
    from . import howto
    return howto.instructions()


def _script(sid):
    """A clientscript's source. The tool already resolves an id or a name."""
    import json
    from .common import registry
    t = registry().get("rs3_script_read")
    if t is None:
        return None
    try:
        got = t["fn"]({"script": sid})
    except Exception as e:
        return ("could not read script %s: %s" % (sid, e), "text/plain")
    body = got.get("source")
    return (body if isinstance(body, str) else json.dumps(got, indent=1, default=str),
            "text/plain")


# uri -> (name, title, description, mimeType, reader)
STATIC = {
    "rs3://guide/tools": (
        "tool routing", "Which tool answers what",
        "The order to reach for the tools in, and which ones answer the same question from "
        "different sides. Sent at initialize too; here so it can be re-read.",
        MD, _instructions),
    "rs3://schema/cache": (
        "cache schema", "What the cache database holds",
        "Tables, row counts and what each one is for, read from the database rather than written "
        "down.",
        MD, lambda: tool_text("rs3_schema")),
}

# (uriTemplate, name, description, mimeType, prefix, handler taking the rest of the uri)
TEMPLATES = [
    ("rs3://script/{id}", "clientscript",
     "One decompiled clientscript by id. Too many to list, so they are addressed. "
     "rs3_script finds the id.", "text/plain", "rs3://script/", _script),
]


def _static():
    out = dict(STATIC)
    for offered in addons.every("resources"):
        out.update(offered().get("static", {}))
    return out


def _templates():
    out = list(TEMPLATES)
    for offered in addons.every("resources"):
        out.extend(offered().get("templates", []))
    return out


def listing():
    """The static resources. Templates are listed separately, as the spec has them."""
    return [{"uri": uri, "name": name, "title": title, "description": desc, "mimeType": mime}
            for uri, (name, title, desc, mime, _read) in _static().items()]


def templates():
    return [{"uriTemplate": t, "name": n, "description": d, "mimeType": m}
            for t, n, d, m, _prefix, _handler in _templates()]


def read(uri):
    """One resource's text, or None when the uri names nothing.

    None is a real answer: the client asked for something we do not have, and an empty string
    would read as an empty file.
    """
    static = _static()
    if uri in static:
        body = static[uri][4]()
        return (body, static[uri][3]) if body is not None else None

    for _t, _n, _d, _m, prefix, handler in _templates():
        if uri.startswith(prefix):
            rest = uri[len(prefix):]
            # No separators. A uri is not a path, and joining one onto a directory is how a
            # reader reaches outside it.
            if not rest or "/" in rest or "\\" in rest or ".." in rest:
                return None
            return handler(rest)
    return None
