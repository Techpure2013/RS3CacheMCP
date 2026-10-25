"""What the server itself was asked, written down for the scrying pool.

The pool draws Claude Code from the transcripts it already keeps. Every other app keeps nothing the
pool can read, so the server notes each tool call and its answer instead, one jsonl per server run
under data/pool/<app>/, in the transcript's own line shapes. journal.py then reads those exactly as
it reads Claude's, and the page needs no idea where a session came from.

What an app does outside rs3cache, its own file edits or shell commands, never reaches the server
and so is not on the map. Claude Code is skipped here, since its transcript already has every call.
"""

import io
import json
import os
import re
import time
from datetime import datetime, timezone

from .common import ROOT

POOL = os.path.join(ROOT, "pool")
# answers are clipped the way the pool clips
CLIP = 8000


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Log:
    def __init__(self):
        self.path = None
        self.seq = 0

    def start(self, client):
        """Open a log for the app that just said hello. Nothing for Claude Code, or with the pool off."""
        client = client or {}
        name = str(client.get("name") or "unknown app")
        if name.startswith("claude-code") or (os.environ.get("RS3_MONITOR") or "").lower() in ("off", "0", "no"):
            return
        folder = os.path.join(POOL, re.sub(r"[^\w.-]+", "-", name).strip("-") or "app")
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            return
        self.path = os.path.join(folder, "%s-%d.jsonl" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid()))
        label = name + (" " + str(client.get("version")) if client.get("version") else "")
        self._line({"type": "custom-title", "customTitle": "%s, through rs3cache" % label})

    def call(self, name, args):
        if not self.path:
            return None
        self.seq += 1
        call_id = "rs3-%d-%d" % (os.getpid(), self.seq)
        self._line({"type": "assistant", "timestamp": _now(), "cwd": os.getcwd(),
                    "message": {"content": [{"type": "tool_use", "id": call_id,
                                             "name": "mcp__rs3cache__" + str(name),
                                             "input": args if isinstance(args, dict) else {}}]}})
        return call_id

    def result(self, call_id, text, error):
        if not call_id or not self.path:
            return
        text = str(text or "")
        if len(text) > CLIP:
            text = text[:CLIP] + "\n... %d more characters" % (len(text) - CLIP)
        self._line({"type": "user", "timestamp": _now(),
                    "message": {"content": [{"type": "tool_result", "tool_use_id": call_id,
                                             "content": text, "is_error": bool(error)}]}})

    def _line(self, rec):
        # a failed write is only a gap here
        try:
            with io.open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        except OSError:
            pass
