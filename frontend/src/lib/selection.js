// Company selection for "Compare selected companies" (pure helpers, tested
// with node --test). The limits come from the backend
// (GET /markets/{m}/sectors -> custom_comparison), which also re-validates
// every request; nothing here is the source of truth for them.

const SYMBOL_RE = /^[A-Z0-9][A-Z0-9&.\-]{0,19}$/;

/** "aapl, MSFT,,msft" -> ["AAPL", "MSFT"] (trimmed, upper-case, unique, valid). */
export function parseSymbols(text) {
  const out = [];
  for (const part of String(text || "").split(",")) {
    const symbol = part.trim().toUpperCase();
    if (SYMBOL_RE.test(symbol) && !out.includes(symbol)) out.push(symbol);
  }
  return out;
}

/** Companies whose symbol or name contains the query (case-insensitive). */
export function filterCompanies(companies, query) {
  const q = String(query || "").trim().toLowerCase();
  const list = Array.isArray(companies) ? companies : [];
  if (!q) return list;
  return list.filter(
    (c) => String(c.symbol || "").toLowerCase().includes(q) || String(c.name || "").toLowerCase().includes(q)
  );
}

/** Add or remove one symbol; returns a new array (order of selection kept). */
export function toggle(selected, symbol) {
  return selected.includes(symbol) ? selected.filter((s) => s !== symbol) : [...selected, symbol];
}

/** Add the visible companies to the selection without exceeding max. */
export function selectAll(filtered, selected, max) {
  const out = [...selected];
  for (const company of filtered) {
    if (out.length >= max) break;
    if (!out.includes(company.symbol)) out.push(company.symbol);
  }
  return out;
}

/**
 * Keep only requested symbols that belong to the sector (e.g. from the URL).
 * Returns { selected, ignored }.
 */
export function initialSelection(requested, members) {
  const known = new Set((members || []).map((m) => m.symbol));
  const selected = [];
  const ignored = [];
  for (const symbol of requested || []) {
    if (known.has(symbol)) {
      if (!selected.includes(symbol)) selected.push(symbol);
    } else if (!ignored.includes(symbol)) {
      ignored.push(symbol);
    }
  }
  return { selected, ignored };
}

/** Counter text and whether Compare is allowed, with the reason when not. */
export function selectionStatus(selected, { min, max }) {
  const count = selected.length;
  const label = `${count} selected (min ${min}, max ${max})`;
  if (count < min) {
    const more = min - count;
    return { count, valid: false, label, reason: `Select at least ${min} companies (${more} more).` };
  }
  if (count > max) {
    const extra = count - max;
    return { count, valid: false, label, reason: `Select at most ${max} companies (remove ${extra}).` };
  }
  return { count, valid: true, label, reason: "" };
}
