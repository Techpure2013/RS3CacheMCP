// Structural search and rewrite over Lua, for the questions a regex answers badly.
//
// Reads one json request on stdin and writes one json answer on stdout, so the python side never
// has to know anything about node. Everything it needs is in the request.
//
// { files: [...], pattern: "...", rewrite: "..."|null, limit: n }
//   -> { matches: [{file, line, col, text, rewritten}], scanned, truncated }

import { readFileSync } from 'node:fs';
import { parse, Lang } from '@ast-grep/napi';

function readStdin() {
  return readFileSync(0, 'utf-8');
}

function main() {
  let req;
  try {
    req = JSON.parse(readStdin() || '{}');
  } catch (e) {
    process.stdout.write(JSON.stringify({ error: 'bad request json: ' + e.message }));
    return;
  }

  const files = req.files || [];
  const pattern = req.pattern || '';
  const rewrite = req.rewrite || null;
  const limit = req.limit || 40;

  if (!pattern) {
    process.stdout.write(JSON.stringify({ error: 'no pattern' }));
    return;
  }

  const matches = [];
  let scanned = 0;
  let truncated = false;

  for (const file of files) {
    if (matches.length >= limit) { truncated = true; break; }
    let src;
    try {
      src = readFileSync(file, 'utf-8');
    } catch {
      continue;                     // a file that moved between listing and reading is not an error
    }
    scanned += 1;

    let found;
    try {
      found = parse(Lang.Lua, src).root().findAll(pattern);
    } catch (e) {
      // A bad pattern is the caller's problem and has to reach them, not be counted as no matches.
      process.stdout.write(JSON.stringify({ error: 'pattern rejected: ' + e.message }));
      return;
    }

    for (const node of found) {
      if (matches.length >= limit) { truncated = true; break; }
      const r = node.range();
      const rec = {
        file,
        line: r.start.line + 1,
        col: r.start.column + 1,
        text: node.text(),
      };
      if (rewrite) {
        try {
          rec.rewritten = node.replace(rewrite).insertedText;
        } catch (e) {
          rec.rewriteError = e.message;
        }
      }
      matches.push(rec);
    }
  }

  process.stdout.write(JSON.stringify({ matches, scanned, truncated }));
}

main();
