import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = fileURLToPath(new URL("../src/", import.meta.url));

function sources(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.(jsx?|mjs)$/.test(name) ? [path] : [];
  });
}

test("no blocking browser dialogs interrupt research, comparison or data flows", () => {
  const offenders = [];
  for (const file of sources(SRC)) {
    readFileSync(file, "utf-8").split("\n").forEach((line, i) => {
      // window.confirm(...) or a bare confirm(...)/alert(...)/prompt(...); obj.method(...) is fine.
      if (/window\.(confirm|alert|prompt)\s*\(|(^|[^.\w])(confirm|alert|prompt)\s*\(/.test(line)) {
        offenders.push(`${file}:${i + 1}: ${line.trim()}`);
      }
    });
  }
  assert.deepEqual(offenders, []);
});
