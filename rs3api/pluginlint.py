"""
Check a plugin manifest against the 21 that are installed and working.

plugin.json decides what the client gives a plugin. A key it does not recognise is ignored without
complaint, which is the same failure the quest linter exists for: `permissions` reads perfectly and
does nothing, because the manifests that work say `requiredPermissions`.

The other half is what the client needs at load. A dependency named in the manifest that is not
installed means the global never appears, and the plugin fails on the first line that uses it. A
`urls` block is what makes HTTP reachable at all; without it the call is refused rather than
failing, which reads like a network problem and is not one.

Everything here is measured off the installed set rather than asserted, so it stays true as the
vocabulary moves.

Tools:
  rs3_plugin_lint  a plugin.json against the ones that work, or the whole installed set
"""

import io
import json
import os

from .common import PLUGINS

# a manifest with none of these fails
REQUIRED = ("name", "version")
_CACHE = {}


def _installed():
    """Every installed manifest, as {plugin folder: parsed json}."""
    if "manifests" in _CACHE:
        return _CACHE["manifests"]
    out = {}
    if os.path.isdir(PLUGINS):
        for d in sorted(os.listdir(PLUGINS)):
            path = os.path.join(PLUGINS, d, "plugin.json")
            if os.path.isfile(path):
                try:
                    out[d] = json.load(io.open(path, encoding="utf-8"))
                except Exception:
                    out[d] = None
    _CACHE["manifests"] = out
    return out


def _vocabulary():
    """Which keys the working manifests use, and how many use each."""
    if "vocab" in _CACHE:
        return _CACHE["vocab"]
    counts, values = {}, {}
    for name, m in _installed().items():
        if not isinstance(m, dict):
            continue
        for k, v in m.items():
            counts[k] = counts.get(k, 0) + 1
            if isinstance(v, list):
                values.setdefault(k, set()).update(str(x) for x in v)
    _CACHE["vocab"] = (counts, values)
    return _CACHE["vocab"]


def _near(key, known):
    """A known key this one is probably a slip for."""
    low = key.lower()
    for k in known:
        kl = k.lower()
        if kl == low or low in kl or kl in low:
            return k
    return None


def tool_plugin_lint(args):
    counts, values = _vocabulary()
    known = set(counts)

    target = str(args.get("path") or "").strip()
    if not target:
        return {"installed": len(_installed()),
                "keysInUse": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
                "permissionsSeen": sorted(values.get("requiredPermissions", set())
                                          | values.get("optionalPermissions", set())),
                "note": "pass path= to a plugin.json, or to a plugin folder. The vocabulary here "
                        "is what the installed manifests actually use, so it moves with them."}

    if os.path.isdir(target):
        target = os.path.join(target, "plugin.json")
    if not os.path.isfile(target):
        return {"error": "no plugin.json at " + target}
    try:
        m = json.load(io.open(target, encoding="utf-8"))
    except ValueError as e:
        return {"path": target, "findings": [{"kind": "not valid json", "detail": str(e)[:120]}],
                "verdict": "the client cannot read this at all"}

    findings = []
    missing = [k for k in REQUIRED if not m.get(k)]
    if missing:
        findings.append({"kind": "required key missing", "keys": missing,
                         "why": "every installed manifest has these"})

    unknown = []
    for k in m:
        if k in known:
            continue
        guess = _near(k, known)
        unknown.append({"key": k, "didYouMean": guess} if guess else {"key": k})
    if unknown:
        findings.append({"kind": "key no working manifest uses", "keys": unknown,
                         "why": "the client ignores a key it does not know, silently"})

    # an uninstalled dependency means its global never appears
    folders = set(_installed())
    for field in ("dependencies", "requiredDependencies", "optionalDependencies"):
        want = m.get(field) or []
        absent = [d for d in want if d not in folders]
        if absent:
            findings.append({"kind": "dependency is not installed", "field": field,
                             "names": absent,
                             "why": "the global never appears and the first use of it fails"})

    # http needs a urls block, or refused
    perms = set(m.get("requiredPermissions") or []) | set(m.get("optionalPermissions") or [])
    if any("http" in p.lower() for p in perms) and not m.get("urls"):
        findings.append({"kind": "http permission with no urls block",
                         "why": "the request is refused rather than failing, which reads like a "
                                "network problem and is not one"})

    seen = values.get("requiredPermissions", set()) | values.get("optionalPermissions", set())
    odd = sorted(p for p in perms if seen and p not in seen)
    if odd:
        findings.append({"kind": "permission no installed plugin asks for", "names": odd,
                         "why": "either new, or spelled differently from the ones that work"})

    return {"path": target, "name": m.get("name"), "version": m.get("version"),
            "findings": findings,
            "verdict": "matches the installed set" if not findings else
                       "%d thing(s) to look at" % len(findings),
            "note": "measured against the %d installed manifests, so a finding is a difference "
                    "from what works rather than proof of a fault." % len(_installed())}


TOOLS = [
    {"name": "rs3_plugin_lint", "fn": tool_plugin_lint, "examples": [{"path": "RS3Plugins/my-plugin"}],
     "description": "Check a plugin.json against the manifests that are installed and working: "
                    "keys the client will silently ignore, a dependency that is not installed so "
                    "its global never appears, and an http permission with no urls block. Omit "
                    "path to see the key vocabulary and the permissions actually in use.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string",
                  "description": "a plugin.json, or the plugin folder holding one"}}}},
]
