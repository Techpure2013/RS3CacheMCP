"""A local classifier in TypeSafe's shape: a state and typed questions in, structured answers out.

Everything runs on the CPU through onnxruntime; nothing leaves the machine. Three engines, and each
answer names the one it used:
  head        logistic regression over a bge-small-en-v1.5 embedding, fitted on our own labels and
              scored on a held-out part of them; the only one marked calibrated
  nli         a DeBERTa-v3-xsmall NLI model reading the text and a statement together: Noul is the
              chance the statement follows. Good at claims against evidence; poor at picking among
              options that are not statements about the text, so a choice uses it only on request
  similarity  embedding closeness to each option, the default for an untrained choice

Request and answer follow TypeSafe's /v1/systemone:
    {"state": "...", "questions": {"q": {"type": "choice", "instructions": "...", "criteria": {...}}}}
    -> {"answers": {"q": {"type": "choice", "choice": "...", "probabilities": {...}, "confidence": 0.8}}}
Choice confidence is theirs: (n * top - 1) / (n - 1).
"""

import hashlib
import json
import math
import os
import re

import numpy as np

from . import common

MODEL_DIR = os.path.join(common.DEFAULT_ROOT, "models", "bge-small-en-v1.5")
# DeBERTa-v3-xsmall NLI, quantised
NLI_DIR = os.path.join(common.DEFAULT_ROOT, "models", "deberta-v3-xsmall-nli")
HEADS = os.path.join(common.DEFAULT_ROOT, "judge", "heads")
MAX_TOKENS = 512
BATCH = 32
# similarity to probability, untrained only
TEMPERATURE = 0.05
HELD_OUT = 0.25
L2 = 1.0

_embedder = None


class Embedder:
    def __init__(self, folder=MODEL_DIR):
        import onnxruntime
        from tokenizers import Tokenizer
        if not os.path.exists(os.path.join(folder, "model.onnx")):
            raise ValueError("no embedding model in %s" % folder)
        self.tok = Tokenizer.from_file(os.path.join(folder, "tokenizer.json"))
        self.tok.enable_truncation(MAX_TOKENS)
        self.tok.enable_padding(pad_id=0, pad_token="[PAD]")
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = min(8, os.cpu_count() or 1)
        self.session = onnxruntime.InferenceSession(os.path.join(folder, "model.onnx"), opts,
                                                    providers=["CPUExecutionProvider"])
        self.feeds = {i.name for i in self.session.get_inputs()}
        self.memo = {}

    def embed(self, texts):
        """Unit vectors, one row per text; each text embedded once per run."""
        keys = [hashlib.sha1(t.encode("utf-8")).hexdigest() for t in texts]
        todo = [(k, t) for k, t in dict(zip(keys, texts)).items() if k not in self.memo]
        for i in range(0, len(todo), BATCH):
            chunk = todo[i:i + BATCH]
            enc = self.tok.encode_batch([t for _, t in chunk])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
            if "token_type_ids" in self.feeds:
                feed["token_type_ids"] = np.zeros_like(ids)
            # bge pools on the first token
            first = self.session.run(None, feed)[0][:, 0]
            first /= np.linalg.norm(first, axis=1, keepdims=True)
            for (k, _), v in zip(chunk, first):
                self.memo[k] = v
        return np.vstack([self.memo[k] for k in keys])


def embedder():
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder


class NLI:
    """How likely a statement is true given a text: the model reads both at once, so it can weigh
    what the text says rather than how alike the words are."""

    def __init__(self, folder=NLI_DIR):
        import onnxruntime
        from tokenizers import Tokenizer
        model = os.path.join(folder, "model_quantized.onnx")
        if not os.path.exists(model):
            raise ValueError("no NLI model in %s" % folder)
        self.tok = Tokenizer.from_file(os.path.join(folder, "tokenizer.json"))
        # the text is cut, never the statement
        self.tok.enable_truncation(MAX_TOKENS, strategy="only_first")
        self.tok.enable_padding(pad_id=0, pad_token="[PAD]")
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = min(8, os.cpu_count() or 1)
        self.session = onnxruntime.InferenceSession(model, opts, providers=["CPUExecutionProvider"])
        cfg = json.load(open(os.path.join(folder, "config.json"), encoding="utf-8-sig"))
        self.yes = int(next(k for k, v in cfg["id2label"].items() if v == "entailment"))

    def entail(self, text, statements):
        """P(statement follows from text), one per statement."""
        out = []
        for i in range(0, len(statements), BATCH):
            enc = self.tok.encode_batch([(text, s) for s in statements[i:i + BATCH]])
            feed = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                    "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
            out.extend(_softmax(self.session.run(None, feed)[0])[:, self.yes].tolist())
        return out


_nli = None


def nli():
    """The NLI model, or None when it is not installed."""
    global _nli
    if _nli is None:
        try:
            _nli = NLI()
        except ValueError:
            _nli = False
    return _nli or None


# does a quote carry its claim
CLAIM_HEAD = os.path.join(HEADS, "claim-holds.json")
CLAIM_FEATURES = ["nli", "nli_min", "cos", "words", "numbers", "names", "ratio"]
_claim_head = None


def claim_head():
    global _claim_head
    if _claim_head is None:
        if not os.path.exists(CLAIM_HEAD):
            raise ValueError("no claim head at %s" % CLAIM_HEAD)
        h = json.load(open(CLAIM_HEAD, encoding="utf-8"))
        if h["features"] != CLAIM_FEATURES:
            raise ValueError("the claim head expects features %s" % h["features"])
        for k in ("mean", "scale", "w"):
            h[k] = np.array(h[k])
        for k in ("clauses", "names", "numbers"):
            h[k] = re.compile(h[k])
        _claim_head = h
    return _claim_head


def holds(quote, claim):
    """How likely a claims-checker is to rule that the quote carries the claim. Use it to rank, not as a
    percentage: on later rulings it still ranked well but under-stated holds by about 20 points.
    Each pair gets its own NLI call: batching pads to the longest pair and moves the score."""
    model = nli()
    if model is None:
        raise ValueError("the claim head needs the NLI model")
    h = claim_head()
    clip = h["logitClip"]

    def logit(p):
        p = min(max(p, clip), 1 - clip)
        return math.log(p / (1 - p))

    parts = [p.strip() for p in h["clauses"].split(claim) if len(p.strip()) > h["clauseMinChars"]] or [claim]
    whole = model.entail(quote, [claim])[0]
    least = min(model.entail(quote, [p])[0] for p in parts)
    cos = float(embedder().embed([quote])[0] @ embedder().embed([claim])[0])
    words = set(re.findall(r"[a-z]{4,}", claim.lower()))
    share = len(words & set(re.findall(r"[a-z]{4,}", quote.lower()))) / max(1, len(words))
    numbers = len(set(h["numbers"].findall(claim)) - set(h["numbers"].findall(quote)))
    names = len({n.strip("`") for n in h["names"].findall(claim)} - {n.strip("`") for n in h["names"].findall(quote)})
    ratio = math.log(max(1, len(claim)) / max(1, len(quote)))
    x = np.array([logit(whole), logit(least), cos, share, math.log1p(numbers), math.log1p(names), ratio])
    z = float(((x - h["mean"]) / h["scale"]) @ h["w"] + h["b"])
    p = 1 / (1 + math.exp(-(h["platt"]["a"] * z + h["platt"]["c"])))
    return {"holds": round(p, 4), "engine": "claim-head", "calibrated": False}


# claim facts the quote lacks
CLAIM_NUMBER = re.compile(
    r"(?<![\w.#-])(?P<v>v)?(?P<n>\d{1,3}(?:,\d{3})+(?![\d])|\d+)(?P<frac>(?:\.\d+)*)"
    r"(?P<mult>\s?[kKM](?![A-Za-z]))?(?P<pct>\s?%| percent\b)?(?!\d)"
    r"|(?<![\w.#-])(?P<v2>v)(?P<ver>\d+(?:\.\d+)+)")
QUOTE_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?!\d)|\d+(?:\.\d+)*")
UNITS = re.compile(r"\s?(?:ms|s|µs|us|ns|kb|mb|gb|kib|mib|gib|b|x|%)(?![A-Za-z])", re.I)
LINE_WORD = re.compile(r"\b(?:line|lines|L)\s?$", re.I)
FILES = re.compile(r"[\w./\\-]*?([\w-]+\.(?:lua|py|rs|js|ts|json|toml|md|txt|exe|ps1|sh))\b")


def _mentions(claim):
    """The counts, percentages and versions a claim states, each with the precision it states them at."""
    out = []
    for m in CLAIM_NUMBER.finditer(claim):
        if m.group("ver"):
            out.append((m.group(0), "version", m.group("ver"), None))
            continue
        n, frac = m.group("n").replace(",", ""), m.group("frac")
        if frac.count(".") > 1 or (m.group("v") and frac):
            out.append((m.group(0), "version", n + frac, None))
            continue
        if m.group("v"):
            continue
        # a name, not a count
        after = claim[m.end():]
        if after[:1].isalpha() and not UNITS.match(after):
            continue
        # a place, not a count
        if claim[:m.start()].endswith(":") or LINE_WORD.search(claim[:m.start()]):
            continue
        value = float(n + frac)
        if value == 0:
            continue
        step = 10.0 ** -len(frac[1:]) if frac else 1.0
        mult = (m.group("mult") or "").strip()
        if mult:
            scale = 1000.0 if mult in "kK" else 1e6
            value, step = value * scale, step * scale
        out.append((m.group(0).strip(), "percent" if m.group("pct") else "number", value, step))
    return out


def _carried(mention, values, versions):
    _, kind, value, step = mention
    if kind == "version":
        return value in versions
    if any(abs(v - value) <= step / 2 + 1e-9 for v in values):
        return True
    if kind == "percent":
        small = [v for v in values if v > 0]
        return any(abs(100 * a / b - value) <= step / 2 + 1e-9 for a in values for b in small if a <= b)
    return False


def unseen(quote, claim):
    """What the claim states that the quote never shows: numbers (allowing separators, k and M,
    rounding at the claim's precision, and percentages of quoted counts) and files it says it ran.
    Advisory: flag a finding when either list is non-empty or holds() is low."""
    values, versions = set(), set()
    for t in QUOTE_NUMBER.findall(quote):
        versions.add(t)
        plain = t.replace(",", "")
        values.update(float(p) for p in (plain.split(".") if plain.count(".") > 1 else [plain]))
    low = quote.lower()
    return {"numbers": [m[0] for m in _mentions(claim) if not _carried(m, values, versions)],
            "files": sorted({f for f in FILES.findall(claim) if f.lower() not in low})}


def _as_text(state):
    return state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)


def confidence(probs):
    n = len(probs)
    return 1.0 if n < 2 else float(max(0.0, min(1.0, (n * max(probs) - 1) / (n - 1))))


def _softmax(z):
    z = z - z.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=-1, keepdims=True)


# heads: logistic regression over the embedding

def train(name, texts, labels, note=""):
    """Fit a head on labelled texts, score it on a held-out quarter, save it. Returns its metrics."""
    classes = sorted(set(labels))
    if len(classes) < 2:
        raise ValueError("a head needs at least two labels")
    x = embedder().embed(list(texts))
    y = np.array([classes.index(l) for l in labels])
    order = np.random.default_rng(0).permutation(len(y))
    cut = int(len(y) * (1 - HELD_OUT))
    fit, test = order[:cut], order[cut:]
    w, b = _fit(x[fit], y[fit], len(classes))
    probs = _softmax(x[test] @ w + b)
    guess = probs.argmax(axis=1)
    majority = np.bincount(y[fit], minlength=len(classes)).argmax()
    metrics = {"examples": len(y), "heldOut": len(test), "accuracy": float((guess == y[test]).mean()),
               "baseline": float((y[test] == majority).mean()), "classes": classes,
               "calibration": _calibration(probs, y[test])}
    w, b = _fit(x, y, len(classes))
    os.makedirs(HEADS, exist_ok=True)
    with open(os.path.join(HEADS, name + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"name": name, "classes": classes, "w": w.tolist(), "b": b.tolist(), "metrics": metrics, "note": note}, fh)
    return metrics


def _fit(x, y, k):
    from scipy.optimize import minimize
    n, d = x.shape
    onehot = np.eye(k)[y]

    def loss(theta):
        w, b = theta[:d * k].reshape(d, k), theta[d * k:]
        p = _softmax(x @ w + b)
        grad = (p - onehot) / n
        value = -np.log(np.clip(p[np.arange(n), y], 1e-12, 1)).mean() + L2 / (2 * n) * (w ** 2).sum()
        gw = x.T @ grad + L2 / n * w
        return value, np.concatenate([gw.ravel(), grad.sum(axis=0)])

    got = minimize(loss, np.zeros(d * k + k), jac=True, method="L-BFGS-B", options={"maxiter": 500})
    return got.x[:d * k].reshape(d, k), got.x[d * k:]


def _calibration(probs, y, bins=5):
    """Expected calibration error: how far stated confidence is from how often it is right."""
    top = probs.max(axis=1)
    right = probs.argmax(axis=1) == y
    err = 0.0
    for lo in np.linspace(0, 1, bins, endpoint=False):
        sel = (top >= lo) & (top < lo + 1 / bins)
        if sel.any():
            err += sel.mean() * abs(top[sel].mean() - right[sel].mean())
    return round(float(err), 3)


def head(name):
    path = os.path.join(HEADS, name + ".json")
    if not os.path.exists(path):
        raise ValueError("no trained head %r" % name)
    h = json.load(open(path, encoding="utf-8"))
    h["w"], h["b"] = np.array(h["w"]), np.array(h["b"])
    return h


# answering

def answer(state, q):
    kind = q.get("type")
    text = _as_text(state)
    engine = None
    if q.get("head"):
        h = head(q["head"])
        vec = embedder().embed([text])[0]
        probs = dict(zip(h["classes"], _softmax(vec @ h["w"] + h["b"]).tolist()))
        engine = "head"
    elif kind == "noul":
        model = nli()
        if model is None:
            raise ValueError("a noul question needs the NLI model or a trained head")
        statement = q.get("instructions") or ""
        return {"type": "noul", "noul": round(model.entail(text, [statement])[0], 4), "engine": "nli", "calibrated": False}
    else:
        criteria = q.get("criteria")
        if isinstance(criteria, list):
            criteria = {str(i): c for i, c in enumerate(criteria)}
        if not criteria:
            raise ValueError("a choice or score needs criteria, or a trained head")
        statements = ["%s: %s" % (q.get("instructions") or "", c) for c in criteria.values()]
        # NLI only when asked
        model =nli() if q.get("engine") == "nli" else None
        if model is not None:
            # entailment per option, normalised
            p =np.array(model.entail(text, statements)) + 1e-9
            probs = dict(zip(criteria, (p / p.sum()).tolist()))
            engine = "nli"
        else:
            sims = embedder().embed(statements) @ embedder().embed([text])[0]
            probs = dict(zip(criteria, _softmax(sims / TEMPERATURE).tolist()))
            engine = "similarity"
    top = max(probs, key=probs.get)
    out = {"type": kind, "probabilities": {k: round(v, 4) for k, v in probs.items()}, "engine": engine,
           "calibrated": engine == "head"}
    if kind == "noul":
        out["noul"] = round(probs.get(True, probs.get("true", probs.get("yes", 0.0))), 4)
    else:
        out[kind if kind in ("choice", "score") else "choice"] = top
        out["confidence"] = round(confidence(list(probs.values())), 4)
    return out


def judge(request):
    """TypeSafe's request shape in, its answer shape out."""
    state, questions = request.get("state"), request.get("questions") or {}
    if state is None or not questions:
        raise ValueError("state and questions are needed")
    return {"model": "local-bge-small", "answers": {name: answer(state, q) for name, q in questions.items()}}


def nearest(text, candidates, k=5):
    """The candidates closest to text, by cosine similarity, best first: [(index, similarity)]."""
    vecs = embedder().embed([text] + list(candidates))
    sims = vecs[1:] @ vecs[0]
    order = np.argsort(-sims)[:k]
    return [(int(i), round(float(sims[i]), 4)) for i in order]
