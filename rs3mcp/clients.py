"""Register the server with the apps that can use it, by writing each one's own config file.

    rs3 setup              which apps are here, and which already have rs3cache
    rs3 setup cursor       add it to one
    rs3 setup all          add it to every app found

Every app keeps MCP servers somewhere different and in a different shape. Most take the same
`mcpServers` json Claude Desktop started; VS Code nests them under `servers`, Zed under
`context_servers`, Codex writes toml and Goose and Continue yaml. The formats were checked against
each app's own documentation on 2026-09-13.

A file that already exists is copied to `<name>.before-rs3cache` the first time it is changed, and
only the rs3cache entry is touched. A file that will not parse, a settings file with comments in it
say, is left alone and the entry is printed to paste by hand.
"""

import io
import json
import os
import shutil
import subprocess
import sys

NAME = "rs3cache"
SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rs3-mcp.py")


def _home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def _appdata(*parts):
    """Where an app keeps its settings: %APPDATA% on Windows, Application Support on a Mac, and
    ~/.config elsewhere."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or _home("AppData", "Roaming")
    elif sys.platform == "darwin":
        base = _home("Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or _home(".config")
    return os.path.join(base, *parts)


def _vscode(*parts):
    return _appdata("Code", "User", *parts)


def entry():
    """The command an app runs to start the server: this python, this server, and the game cache
    location when it is not the default."""
    rec = {"command": sys.executable.replace("\\", "/"), "args": [SERVER.replace("\\", "/")]}
    env = {k: os.environ[k] for k in ("RS3MCP_GAME_CACHE", "RS3MCP_ADDONS", "RS3MCP_DATA")
           if os.environ.get(k)}
    if env:
        rec["env"] = env
    return rec


# key, label, folder whose existence says the app is installed, config file, format
APPS = [
    ("claude-code", "Claude Code", lambda: _home(".claude"), lambda: _home(".claude.json"), "claude-code"),
    ("claude-desktop", "Claude Desktop", lambda: _appdata("Claude"),
     lambda: _appdata("Claude", "claude_desktop_config.json"), "mcpServers"),
    ("cursor", "Cursor", lambda: _home(".cursor"), lambda: _home(".cursor", "mcp.json"), "mcpServers"),
    ("windsurf", "Windsurf", lambda: _home(".codeium", "windsurf"),
     lambda: _home(".codeium", "windsurf", "mcp_config.json"), "mcpServers"),
    ("vscode", "VS Code (Copilot agent mode)", lambda: _vscode(), lambda: _vscode("mcp.json"), "vscode"),
    ("cline", "Cline (VS Code extension)", lambda: _vscode("globalStorage", "saoudrizwan.claude-dev"),
     lambda: _vscode("globalStorage", "saoudrizwan.claude-dev", "settings", "cline_mcp_settings.json"),
     "mcpServers"),
    ("continue", "Continue", lambda: _home(".continue"),
     lambda: _home(".continue", "mcpServers", "rs3cache.yaml"), "continue"),
    ("zed", "Zed", lambda: _appdata("Zed") if os.name == "nt" else _home(".config", "zed"),
     lambda: (_appdata("Zed") if os.name == "nt" else _home(".config", "zed")) + os.sep + "settings.json",
     "zed"),
    ("gemini", "Gemini CLI", lambda: _home(".gemini"), lambda: _home(".gemini", "settings.json"), "mcpServers"),
    ("codex", "Codex CLI and the ChatGPT desktop app", lambda: _home(".codex"),
     lambda: _home(".codex", "config.toml"), "codex"),
    ("goose", "Goose", lambda: _appdata("Block", "goose") if os.name == "nt" else _home(".config", "goose"),
     lambda: (os.path.join(_appdata("Block", "goose"), "config") if os.name == "nt"
              else _home(".config", "goose")) + os.sep + "config.yaml", "goose"),
    ("lmstudio", "LM Studio", lambda: _home(".lmstudio"), lambda: _home(".lmstudio", "mcp.json"), "mcpServers"),
]
BY_KEY = {a[0]: a for a in APPS}


def _read(path):
    try:
        return io.open(path, encoding="utf-8").read()
    except OSError:
        return None


def _backup(path):
    keep = path + ".before-rs3cache"
    if os.path.exists(path) and not os.path.exists(keep):
        shutil.copy2(path, keep)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _backup(path)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _json_with(path, outer, rec):
    """(status, snippet): rec under outer.NAME in a json file, creating the file if need be."""
    text = _read(path)
    try:
        doc = json.loads(text) if text and text.strip() else {}
    except ValueError:
        return _jsonc_with(path, text, outer, rec)
    servers = doc.setdefault(outer, {})
    if servers.get(NAME) == rec:
        return "already", None
    servers[NAME] = rec
    _write(path, json.dumps(doc, indent=2) + "\n")
    return "added", None


def _jsonc_with(path, text, outer, rec):
    """The same for a settings file with comments, which json cannot read back and write out
    without losing them. The entry goes in as text, straight after the brace that opens `outer`,
    or after the file's own first brace with `outer` around it. Both Zed and VS Code read the
    trailing comma that can leave."""
    import re
    snippet = json.dumps({outer: {NAME: rec}}, indent=2)
    if '"%s"' % NAME in text:
        return "already", None
    body = json.dumps(rec, indent=2).replace("\n", "\n    ")
    found = re.search(r'"%s"\s*:\s*\{' % re.escape(outer), text)
    if found:
        at = found.end()
        text = text[:at] + '\n    "%s": %s,' % (NAME, body) + text[at:]
    elif "{" in text:
        at = text.index("{") + 1
        text = text[:at] + '\n  "%s": {\n    "%s": %s\n  },' % (outer, NAME, body) + text[at:]
    else:
        return "manual", snippet
    _write(path, text)
    return "added", None


def status(key):
    """'here' when the app looks installed, 'set' when rs3cache is already in its config, else None."""
    _k, _label, folder, config, fmt = BY_KEY[key]
    text = _read(config()) or ""
    if fmt == "claude-code":
        if _which("claude"):
            try:
                out = subprocess.run(["claude", "mcp", "get", NAME], capture_output=True, text=True,
                                     timeout=20, shell=os.name == "nt")
                if out.returncode == 0:
                    return "set"
            except (OSError, subprocess.SubprocessError):
                pass
        return "set" if '"%s"' % NAME in text else ("here" if os.path.isdir(folder()) else None)
    if (NAME + ":" in text) or ('"%s"' % NAME in text) or ("mcp_servers.%s]" % NAME in text):
        return "set"
    return "here" if os.path.isdir(folder()) else None


def _which(cmd):
    return shutil.which(cmd)


def add(key):
    """(status, where, snippet). status is added, already or manual; a manual one comes with the
    entry to paste."""
    _k, label, _folder, config, fmt = BY_KEY[key]
    path, rec = config(), entry()
    if fmt == "mcpServers":
        got, snip = _json_with(path, "mcpServers", rec)
        return got, path, snip
    if fmt == "vscode":
        got, snip = _json_with(path, "servers", dict(rec, type="stdio"))
        return got, path, snip
    if fmt == "zed":
        got, snip = _json_with(path, "context_servers", rec)
        return got, path, snip
    if fmt == "claude-code":
        if _which("claude"):
            argv = ["claude", "mcp", "add", "--scope", "user", NAME]
            for k, v in (rec.get("env") or {}).items():
                argv += ["-e", "%s=%s" % (k, v)]
            argv += ["--", rec["command"]] + rec["args"]
            done = subprocess.run(argv, capture_output=True, text=True, timeout=60, shell=os.name == "nt")
            if done.returncode == 0:
                return "added", "claude mcp add", None
            if "already exists" in (done.stdout + done.stderr):
                return "already", "claude mcp", None
        got, snip = _json_with(path, "mcpServers", dict(rec, type="stdio"))
        return got, path, snip
    if fmt == "codex":
        text = _read(path) or ""
        if "[mcp_servers.%s]" % NAME in text:
            return "already", path, None
        block = "\n[mcp_servers.%s]\ncommand = %s\nargs = [%s]\n" % (
            NAME, json.dumps(rec["command"]), ", ".join(json.dumps(a) for a in rec["args"]))
        if rec.get("env"):
            block += "env = { %s }\n" % ", ".join("%s = %s" % (k, json.dumps(v)) for k, v in rec["env"].items())
        _write(path, text.rstrip("\n") + ("\n" if text.strip() else "") + block)
        return "added", path, None
    if fmt == "goose":
        return _goose(path, rec)
    if fmt == "continue":
        body = ["name: rs3cache", "version: 0.0.1", "schema: v1", "mcpServers:",
                "  - name: rs3cache", "    command: %s" % json.dumps(rec["command"]), "    args:"]
        body += ["      - %s" % json.dumps(a) for a in rec["args"]]
        if rec.get("env"):
            body.append("    env:")
            body += ["      %s: %s" % (k, json.dumps(v)) for k, v in rec["env"].items()]
        text = "\n".join(body) + "\n"
        if _read(path) == text:
            return "already", path, None
        _write(path, text)
        return "added", path, None
    raise ValueError(label)


def _goose(path, rec):
    """Goose's yaml has no parser in the standard library, so the entry goes in as text: under an
    existing top level `extensions:` at the indent its first child uses, or as a new section."""
    text = _read(path) or ""
    lines = text.splitlines()
    if any(l.strip() == NAME + ":" for l in lines):
        return "already", path, None

    def block(pad):
        out = ["%s%s:" % (pad, NAME), "%s  name: rs3cache" % pad, "%s  type: stdio" % pad,
               "%s  cmd: %s" % (pad, json.dumps(rec["command"])),
               "%s  args: [%s]" % (pad, ", ".join(json.dumps(a) for a in rec["args"])),
               "%s  enabled: true" % pad, "%s  timeout: 300" % pad]
        if rec.get("env"):
            out.append("%s  envs: { %s }" % (pad, ", ".join("%s: %s" % (json.dumps(k), json.dumps(v))
                                                           for k, v in rec["env"].items())))
        return out

    at = next((i for i, l in enumerate(lines) if l.rstrip() == "extensions:"), None)
    if at is None and any(l.startswith("extensions:") for l in lines):
        # written inline, `extensions: {}`, which text insertion would break
        return "manual", path, "\n".join(["extensions:"] + block("  "))
    if at is None:
        lines += ["extensions:"] + block("  ")
    else:
        child = next((l for l in lines[at + 1:] if l.strip()), "")
        pad = child[:len(child) - len(child.lstrip())] or "  "
        lines[at + 1:at + 1] = block(pad)
    _write(path, "\n".join(lines) + "\n")
    return "added", path, None
