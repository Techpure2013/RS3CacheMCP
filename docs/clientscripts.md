# Clientscripts

[All docs](README.md) › How it works

The game runs about 21,000 small scripts for things like interfaces, quest journals, chat and the
world map. rs3cache turns every one back into code you can read.

That answers things nothing else in the cache can: what changes a varbit, what a value means, or
which interface a script opens.

To use them:

```
rs3 script <text>                      search every script
rs3 script_read <id or name>           read one
rs3 script_refs kind=varbit id=<n>     find every script that uses a varbit
```

`script_refs` also works for vars, interfaces and items. The scripts themselves are saved as
`data/rs3-cache-stage/clientscript/clientscript-<id>.ts`.

## What a script looks like

```ts
//
function script13621(int0: obj): int {
    switch (int0) {
        case 1037:
            return testbit(varplayer_413, 1);
        case 6856:
            if (varbitplayer_14444 >= 1) {
                return 1;
            }
            break;
    }
    ...
}
```

- The comment at the top is the script's name, like `//[proc,quicksort]`. It's empty when the name
  isn't known.
- Arguments and local variables are called `int0`, `string0`, `long0` and so on.
- Vars look like `varplayer_413`, `varbitplayer_14444` or `varclient_2835`.
- Ids are marked with their type, like `4151 as obj`. Components look like `comp(1494, 36)`,
  coordinates like `coord(0, 3222, 3218)`, colours are in hex, and -1 is `null`.
- A call to another script looks like `script9727(...)`. `callback(script688, ...)` means the script
  is set up to run later.
- A script that returns more than one value looks like `[int2, int3] = script8546(int0);`.
- `clientvars.d.ts` lists every var and its type, so a code editor can follow them.

The search index relies on these patterns, so they don't change.

## How it works

1. **Reading.** `rs3mcp/clientscript.py` reads each script's instructions out of the cache.
   `rs3mcp/opcodes/clientscript-opmap.json` says what each instruction number means.
2. **Stack.** `rs3mcp/csstack.py` knows what each instruction takes and gives back. Where that isn't
   known, it works it out from the scripts. The stack is always empty between statements, so a
   statement with just one unknown instruction shows what that instruction must do.
3. **Types.** `rs3mcp/cstypes.py` works out whether each value is a number, an item, a string and so
   on, by looking at how it's used across every script. It goes round until nothing changes.
4. **Structure.** `rs3mcp/csdecomp.py` turns the jumps back into `if`, `while` and `switch`.
5. **Checking.** It then checks that the result jumps exactly where the original did. If it doesn't,
   that script is written out with gotos instead, which is always correct.
6. **Second try.** A script that still needed gotos gets another go, with small shared bits of code
   copied into each place that uses them. The result is kept only if it's better and passes the check.

## Where it stands

All 21,098 scripts come out as normal code, with no gotos. About 31 ids are still untyped. They're
all values whose type depends on what happens while the game runs.

## Game updates

Every game update shuffles the instruction numbers, so the old map stops working. `rs3 rebuild` works
out the new numbers by itself.

To do that, rs3cache keeps a copy of the scripts from the last update it read, in
`rs3mcp/opcodes/clientscript-reference.bin` (2.3 MB). Most scripts don't change in an update apart
from their instruction numbers. So lining each new script up with its old copy shows which new number
matches which old one. A number that matches nothing old is a new instruction, and it gets worked out
like any other unknown one.

When the rebuild finds a new update, it:

- saves the new map, but only if every script can be read with it and at least 60% of scripts were
  otherwise unchanged. The old map is kept as `data/clientscript-opmap.previous.json`.
- turns the scripts back into code with it. If a number was matched wrong, the scripts using it won't
  add up, and the new database isn't installed.
- makes this update the new reference once every script works, so the next update is compared with
  this one.

If the numbers can't be worked out, it installs the rest of the database and keeps the old
scripts.

So far this has only been tested on fake updates: this cache with every number shuffled, and some
scripts changed, removed or added. With 3%, 10% and 25% of scripts changed, it got every number
right. With half changed it couldn't, and the 60% rule stopped it saving a bad map. The first real
game update will be its first real test.

## Working on it

```
python rs3mcp/clientscript.py verify           checks the map can read every script
python rs3mcp/clientscript.py dis <id>         shows one script's raw instructions
python rs3mcp/csstack.py verify                checks the stack adds up in every script
python rs3mcp/csstack.py infer --save          works out unknown instructions and saves them
python rs3mcp/csstack.py trace <id> [at]       shows the stack before each instruction
python rs3mcp/csdecomp.py <id>                 turns one script back into code
python rs3mcp/csdecomp.py check                checks every script, and counts gotos and failures
python rs3mcp/csdecomp.py write <folder>       writes every script to files
python rs3mcp/cscalibrate.py check             checks the map and the reference match the cache
python rs3mcp/cscalibrate.py run               works out the numbers for this cache and saves them
python rs3mcp/cscalibrate.py snapshot          makes the current cache the reference
python rs3mcp/cscalibrate.py simulate [seed] [share]   fakes an update and checks it gets worked out
```

`check` and `write` use every CPU core. The result is the same as running on one.

---

Previous: [Config specs](specs.md) · Next: [Models](models.md)
