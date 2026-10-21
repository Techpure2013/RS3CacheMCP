# Installing

[All docs](README.md) › Using it

## What you need

- **Windows.** rs3cache reads the cache the Windows game client downloads. It lives in
  `C:\ProgramData\Jagex\RuneScape` unless you moved it.
- **RuneScape 3, logged in at least once**, so the game has downloaded its cache.
- **Python 3.10 or newer.** It's been tested on 3.12 and 3.13. When you install Python from
  python.org, tick "Add python.exe to PATH".
- **About 7 GB of free space.** Most of that is sprites, plus a backup copy kept each time you
  rebuild.

That's all rs3cache needs. If you want to draw item icons or model pictures, also run
`pip install pillow numpy`. For the client API docs and their Lua typings, which a rebuild
fetches at the end, run `pip install beautifulsoup4`.

## First setup

Open a terminal in the rs3cache folder and run these three commands:

```
rs3 doctor
rs3 rebuild
rs3 setup
```

1. `rs3 doctor` checks your install and finds the game cache. If something is missing, it tells you
   how to fix it.
2. `rs3 rebuild` builds the database from your game cache. It takes about three or four minutes.
3. `rs3 setup` shows the AI apps you have. Run `rs3 setup <app>` to add rs3cache to one, or
   `rs3 setup all` for all of them. Then restart the app. See [AI apps](apps.md) for more.

In PowerShell, put `.\` in front: `.\rs3 doctor`.

To run `rs3` from any folder, add the rs3cache folder to your PATH. Open Start, search for "Edit
environment variables for your account", select Path, click New, and paste the folder's path.

If you moved your game cache, set `RS3MCP_GAME_CACHE` to where it is before you run anything.

## After a game update

Run:

```
rs3 rebuild
```

If the game hasn't updated, it does nothing. `rs3 doctor` also tells you when a rebuild is due.

A rebuild never makes things worse. If the new database reads less than the old one, it keeps the
old one. See [how the database is built](rebuild.md) for what it checks.

Sometimes a game update breaks the clientscripts, because Jagex shuffles their instruction numbers.
If that happens, the rebuild still updates everything else and tells you the scripts were skipped.
See [clientscripts](clientscripts.md).

## Removing it

1. **Close every AI app that uses rs3cache.** Windows won't delete the folder while an app is using
   it.

2. **Remove it from each app you set up.**

   | App | What to do |
   |---|---|
   | Claude Code | run `claude mcp remove rs3cache --scope user` |
   | Continue | delete `~\.continue\mcpServers\rs3cache.yaml` |
   | Any other | open its config file (see [AI apps](apps.md)) and delete just the `rs3cache` entry |

   Setup also left a backup next to each config file, called `<file>.before-rs3cache`. You can
   delete it once the entry is gone. Don't copy it back over your config: you'd lose any changes
   you've made since.

3. **Delete the rs3cache folder.** Everything rs3cache built is in its `data` folder. If you set
   `RS3MCP_DATA` to put the data somewhere else, delete that folder too.

4. **Undo anything you set up by hand.** Take rs3cache out of your PATH if you added it, and remove
   any `RS3MCP_` or `RS3_` environment variables you set.

That's everything. Apart from your app configs, rs3cache never writes anything outside its own
folder.

---

Next: [The rs3 command](cli.md)
