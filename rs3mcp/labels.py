"""Labelled claim-and-output pairs, the yardstick for the handback gate's second opinion.

The pool is built from past agents' handbacks: each claim that something ran, passed or was
verified, beside the output of what actually ran (commands the claim names first, then the closest
by meaning; the end of each output, where results print). Picked evenly across the NLI score's
range, so a threshold is measured where the model is sure and where it is not.

Labels are Techpure's: supported, unsupported or unclear. A suggestion from a plain rule (a pass
count in the output, a traceback, nothing run) is shown but never counted until it is confirmed.
Labels live in an append-only log; the latest per pair wins, so an answer can be taken back.
"""

import hashlib
import io
import json
import os
import random
import re
import threading
import time

from . import common

FOLDER = os.path.join(common.DEFAULT_ROOT, "judge", "labels")
POOL = os.path.join(FOLDER, "claims-pool.jsonl")
LOG = os.path.join(FOLDER, "claims-labels.jsonl")
POOL_SIZE = 200
BUCKETS = (0.0, 0.1, 0.2, 0.4, 0.6, 0.8, 1.01)
VALUES = ("supported", "unsupported", "unclear")
SHOW_CHARS = 1500
THRESHOLDS = (0.05, 0.1, 0.2, 0.3, 0.4, 0.5)

PASSED = re.compile(r"(\d+)\s+(?:passed|checks passed|ok)\b", re.I)
FAILED = re.compile(r"(\d+)\s+(?:failed|FAILED|errors?)\b")
BROKE = re.compile(r"Traceback \(most recent call last\)|\bError:|\bFAILED\b|exited with code [1-9]")

_build = {"running": False, "done": 0, "total": 0, "error": None}


def _key(agent, claim):
    return hashlib.sha1(("%s|%s" % (agent, claim)).encode("utf-8")).hexdigest()[:12]


def suggest(claim, outputs):
    """A plain-rule guess, with the reason, or None. Never a label on its own."""
    if not outputs:
        return {"value": "unsupported", "why": "nothing was run"}
    # read the last run that reports a count
    counted = [o for o in outputs if PASSED.search(o["text"]) or FAILED.search(o["text"])]
    text = (counted or outputs)[-1]["text"]
    says_pass = re.search(r"\bpass(?:ed|es)?\b|\bgreen\b|\bno failures\b", claim, re.I)
    fails = [int(n) for n in FAILED.findall(text)]
    if says_pass and fails and max(fails) > 0:
        return {"value": "unsupported", "why": "the output reports %d failed" % max(fails)}
    if says_pass and PASSED.search(text) and not (fails and max(fails) > 0) and not BROKE.search(text):
        return {"value": "supported", "why": "the output reports passes and no failures"}
    if says_pass and BROKE.search(text):
        return {"value": "unsupported", "why": "the output shows an error"}
    return None


def build(size=POOL_SIZE, seed=0):
    """Score every past handback's run claims and keep a pool spread over the score's range."""
    import glob
    from . import handback
    files = glob.glob(os.path.join(handback.PROJECTS, "*", "*", "subagents", "**", "agent-*.jsonl"), recursive=True)
    _build.update(running=True, done=0, total=len(files), error=None)
    rows = []
    try:
        for path in files:
            _build["done"] += 1
            last = None
            for line in io.open(path, encoding="utf-8", errors="replace"):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") == "assistant":
                    for b in (r.get("message") or {}).get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "text" and "handback" in b.get("text", "").lower():
                            last = b["text"]
            if not last:
                continue
            agent = os.path.basename(path)[6:-6]
            for item in _pairs(last, path):
                rows.append(dict(item, agent=agent, id=_key(agent, item["claim"])))
        rng = random.Random(seed)
        per = max(1, size // (len(BUCKETS) - 1))
        pool = []
        for lo, hi in zip(BUCKETS, BUCKETS[1:]):
            bucket = [r for r in rows if lo <= r["nli"] < hi]
            rng.shuffle(bucket)
            pool += bucket[:per]
        rng.shuffle(pool)
        os.makedirs(FOLDER, exist_ok=True)
        with io.open(POOL, "w", encoding="utf-8", newline="\n") as fh:
            for r in pool:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        return {"pool": len(pool), "scored": len(rows)}
    except Exception as e:
        _build["error"] = "%s: %s" % (type(e).__name__, e)
        raise
    finally:
        _build["running"] = False


def _pairs(message, transcript):
    """Each run claim with its best outputs and the NLI score, as second_opinion sees them."""
    from . import handback, judge
    claims, evidence = handback.claims_of(message), handback._evidence(transcript)
    out = []
    if not claims:
        return out
    model = judge.nli()
    vecs = judge.embedder().embed(evidence) if evidence else None
    for claim in claims:
        if not evidence:
            out.append({"claim": claim, "outputs": [], "nli": 0.0})
            continue
        near = list((vecs @ judge.embedder().embed([claim])[0]).argsort()[::-1][:handback.NEAREST])
        named = [i for i, e in enumerate(evidence) if any(len(w) > 4 and w in e.split("\n", 1)[0]
                                                          for w in re.findall(r"[\w./-]+", claim))]
        picked = list(dict.fromkeys(named[-handback.NEAREST:] + near))[:handback.NEAREST]
        scores = [model.entail(evidence[i], [claim])[0] for i in picked] if model else [0.0]
        outputs = [{"cmd": evidence[i].split("\n", 1)[0][:300], "text": evidence[i].split("\n", 1)[-1][-SHOW_CHARS:]}
                   for i in picked]
        out.append({"claim": claim[:600], "outputs": outputs, "nli": round(max(scores), 3)})
    return out


def start_build(args):
    if _build["running"]:
        return {"building": True}
    threading.Thread(target=build, kwargs={"size": int(args.get("size") or POOL_SIZE)}, daemon=True).start()
    return {"building": True}


def _labels():
    latest = {}
    try:
        for line in io.open(LOG, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            latest[r["id"]] = r
    except OSError:
        pass
    return {k: v for k, v in latest.items() if v.get("value")}


def _pool():
    try:
        return [json.loads(l) for l in io.open(POOL, encoding="utf-8") if l.strip()]
    except OSError:
        return []


def state(args):
    """The pool with each pair's label and suggestion, the build's progress, and the measurements."""
    pool, labels = _pool(), _labels()
    items = []
    for r in pool:
        lab = labels.get(r["id"])
        items.append(dict(r, label=lab["value"] if lab else None, suggestion=suggest(r["claim"], r["outputs"])))
    return {"items": items, "build": dict(_build), "counts": {v: sum(1 for i in items if i["label"] == v) for v in VALUES},
            "measure": measure(items)}


def label(args):
    """{id, value, by}: append one answer; value null takes it back."""
    value = args.get("value")
    if value not in VALUES + (None,):
        raise ValueError("value is one of %s, or null to take it back" % ", ".join(VALUES))
    os.makedirs(FOLDER, exist_ok=True)
    with io.open(LOG, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"id": args.get("id"), "value": value, "by": args.get("by") or "?",
                             "at": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
    return {"ok": True}


def measure(items):
    """For each threshold: flags raised (score below it) and how many of them are truly unsupported."""
    done = [i for i in items if i["label"] in ("supported", "unsupported")]
    if not done:
        return {"labelled": 0, "rows": []}
    unsupported = sum(i["label"] == "unsupported" for i in done)
    rows = []
    for t in THRESHOLDS:
        flagged = [i for i in done if i["nli"] < t]
        right = sum(i["label"] == "unsupported" for i in flagged)
        rows.append({"threshold": t, "flagged": len(flagged), "right": right,
                     "precision": round(right / len(flagged), 3) if flagged else None,
                     "recall": round(right / unsupported, 3) if unsupported else None})
    return {"labelled": len(done), "unsupported": unsupported, "rows": rows}
