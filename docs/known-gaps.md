# Known gaps

[All docs](README.md) › How it works

Things rs3cache can't do yet, or can only do with some help. When one gets fixed, it comes off this
list.

## The cache

**Clientscripts haven't been through a real game update yet.** Every update shuffles the script
instruction numbers, and the rebuild now works them out on its own ([clientscripts](clientscripts.md)).
It gets them all right on fake updates where up to a quarter of scripts changed. The next real update
will be the real test.

**Most sprites don't have names.** The cache only stores a hash of each name, so names have to be
guessed and checked. With the name lists, which now ship, the rebuild finds 573 of the 36,425.
Without them it finds 57. Names found in earlier builds are kept, so there are 669 here. You can
find every sprite by id, named or not.

**The name lists go stale on their own.** They ship as `rs3mcp/names` and are not rebuilt from your
cache, so a config added after the sheets were taken has an id and no name. Where a sheet and the
cache disagree about a varbit's shape the cache wins; the sheets only lend names.

**Everything reads.** Every config, model, clientscript and sprite can be read, and every clientscript
comes out as normal code. About 31 ids are still untyped, because their type depends on what
happens while the game runs.

**No 3D viewer.** You can save a model as a picture or an obj file ([commands](commands.md)), but you
can't spin it around. A viewer needs materials, textures and animation, and hasn't been started.

## Item icons

`rs3-icon.py` needs Pillow and numpy. Some textures are only in the cache in a compressed form. That
costs about a level and a half out of 255 per colour, measured on 9,257 textures that are stored both
ways, and it doesn't lean lighter or darker. That's too little to see, so if an icon looks off, the
texture isn't why.

A few texture ids hold a different picture in each form, like a grey copy and a coloured one. Which
one the game draws isn't known yet.

## Other computers

**Only tested in Windows Sandbox so far.** In a fresh Windows Sandbox with just Python and the game
cache, the whole install works: doctor, the first build, setup, an AI app, the scrying pool and all
the tests. It hasn't been tried on someone else's actual PC, with a different game cache, a slower
disk or a different antivirus.

**Antivirus slows down the first index.** Windows Defender scans each of the 21,000 script files the
first time they're opened. Excluding the `data` folder speeds it up. That's your call.

## Apps

**Browser-only apps can't use it.** ChatGPT in a browser, and apps like it, can't start programs on
your computer. The ChatGPT desktop app can, through Codex.

**The scrying pool only sees part of other apps.** For Claude Code it sees everything. For other apps
it only sees their rs3cache calls. See [the scrying pool](scrying-pool.md).

---

Previous: [Testing](testing.md)
