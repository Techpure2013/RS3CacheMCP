# Testing

[All docs](README.md) › How it works

## Which to run

| After you | Run |
|---|---|
| change a tool | `python rs3-mcp.py --selftest` |
| change something that shouldn't change any answers | `python rs3-mcp.py --diff` |
| rebuild | nothing. The rebuild runs the tests itself |
| change how something is read from the cache | `rs3 rebuild --force`, then the selftest |
| want to know how fast things are | `python rs3mcp/test/bench.py`, or `latency.py` |

## The tests

**`python rs3-mcp.py --selftest`** tries a set of tools and prints one line for each. Then it runs
all the checks below. It exits with 1 if anything fails.

**`python rs3-mcp.py --test`** runs just the checks, from `rs3mcp/test/correctness.py` and any
add-ons. There are a few hundred of them. They cover the server, the `rs3` command, app setup, the
scrying pool, reading the cache, the database, the search index, the wiki, these docs and their
links, and old bugs so they don't come back.

Three of the checks keep the text honest:

- **Claims.** When the code says a number about the data, like "sprite_hash holds all 36,425
  hashes", a check compares it with the database. If the number is wrong, it fails.
- **Written numbers.** The tests count every number written into the code's text. A new one fails
  until someone checks it and raises the count. That way no unchecked number slips in.
- **Tool list size.** The tool list apps receive has a size limit. Going over it has to be done on
  purpose.

`rs3 drift` runs the claims check as a tool.

**`python rs3-mcp.py --golden`** saves every tool's answer to a set of questions. **`--diff`**
compares the current answers with the saved ones and shows what changed. After a rebuild some
answers change on purpose, and this shows you which.

The saved answers are in `rs3mcp/test/golden/`. Some answers need things a fresh install doesn't
have, like name lists or add-on tools. An add-on can keep those in its own folder.

**`python rs3mcp/test/soak.py`** calls every tool hundreds of times with real ids and names from the
database, looking for crashes and slow calls. Add `--deep` for thousands, or `--seed N` to repeat a
run exactly.

**`python rs3mcp/test/bench.py`** times every tool.

**`python rs3mcp/test/latency.py`** compares each tool's first call with its second. That shows
which tools save their work and which redo it every time.

**`python rs3mcp/test/sandbox.py`** lets you try a change without touching the copy you're using:

```
python rs3mcp/test/sandbox.py new <name>        make a copy to work on
python rs3mcp/test/sandbox.py check <name>      test it, and compare its answers with yours
python rs3mcp/test/sandbox.py files <name>      show what's different
python rs3mcp/test/sandbox.py promote <name>    copy it back, only if the tests pass
python rs3mcp/test/sandbox.py drop <name>       throw it away
```

## On another computer

All the tests pass on a computer that just has RuneScape and a database built with `rs3 rebuild`.

A few checks need more than that: the name lists, an older database from a previous rebuild, or an
add-on. `rs3mcp/test/workspace.py` lists what each one needs. If it's missing, the check is skipped,
and the end of the run tells you why:

```
skipped, for want of what this machine does not have:
   a previous database, which only a second rebuild leaves: cache diff
346 checks passed
```

`rs3_drift` and the saved answers skip the same way. If you do have everything, nothing is skipped.
So a skip on your main computer means something has gone missing.

The saved answers were recorded on one computer with one game cache. On another computer, or after
a game update, expect some to differ. Run `--golden` to save your own.

---

Previous: [Add-ons](addons.md) · Next: [Known gaps](known-gaps.md)
