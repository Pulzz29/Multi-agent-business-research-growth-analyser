// Centralised company-logo resolution.
//
// Logo candidates come only from the backend (/api/v1/logos), which derives
// them from each company's own stored provider profile: the provider's logo
// for exactly that symbol, then the company's official website icons. The UI
// tries them in order and falls back to an initials badge. No per-company
// URLs are kept here and logos are never matched by company name.

const cache = new Map(); // symbol -> { candidates: string[] } (session lifetime)
const listeners = new Map(); // symbol -> Set<fn>
let queue = new Set();
let timer = null;
let fetcher = null;

/** Configure how metadata is fetched: (symbols) => Promise<{ logos: {SYM: meta} }>. */
export function setLogoFetcher(fn) {
  fetcher = fn;
}

export function resetLogoCache() {
  cache.clear();
  queue = new Set();
}

export function validLogoUrl(url) {
  try {
    const u = new URL(String(url));
    return u.protocol === "https:" && Boolean(u.hostname) && u.pathname.length > 1;
  } catch {
    return false;
  }
}

/** Ordered, de-duplicated https candidates from backend metadata. */
export function logoCandidates(meta) {
  const seen = new Set();
  const out = [];
  for (const c of meta?.candidates || []) {
    if (validLogoUrl(c?.url) && !seen.has(c.url)) {
      seen.add(c.url);
      out.push(c.url);
    }
  }
  return out;
}

/** Initials for the fallback badge: two words of the name, else the ticker's first letters. */
export function initialsFor(name, symbol) {
  const words = String(name || "")
    .replace(/\b(limited|ltd|inc|corp|corporation|company|co|plc|the|and)\b\.?/gi, " ")
    .split(/[^A-Za-z0-9]+/)
    .filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return String(symbol || "?").replace(/\..*$/, "").slice(0, 2).toUpperCase();
}

/** Deterministic background colour per symbol (stable across renders). */
export function markColor(symbol) {
  let h = 0;
  for (const ch of String(symbol || "")) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `hsl(${h} 45% 38%)`;
}

function flush() {
  timer = null;
  const symbols = [...queue].filter((s) => !cache.has(s)).slice(0, 100);
  queue = new Set();
  if (!symbols.length || !fetcher) return;
  fetcher(symbols)
    .then((data) => {
      for (const s of symbols) {
        cache.set(s, { candidates: logoCandidates(data?.logos?.[s]) });
        (listeners.get(s) || []).forEach((fn) => fn(cache.get(s)));
      }
    })
    .catch(() => {
      for (const s of symbols) {
        cache.set(s, { candidates: [] });
        (listeners.get(s) || []).forEach((fn) => fn(cache.get(s)));
      }
    });
}

/** Subscribe to a symbol's logo metadata; requests are batched. Returns unsubscribe. */
export function subscribeLogo(symbol, fn) {
  const key = String(symbol || "").toUpperCase();
  if (!key) return () => {};
  if (!listeners.has(key)) listeners.set(key, new Set());
  listeners.get(key).add(fn);
  if (cache.has(key)) fn(cache.get(key));
  else {
    queue.add(key);
    if (!timer) timer = setTimeout(flush, 30);
  }
  return () => listeners.get(key)?.delete(fn);
}
