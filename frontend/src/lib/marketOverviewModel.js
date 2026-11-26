// Overview market toggle: selection persistence and view models (pure helpers).

export const MARKET_KEY = "insightiq-market";
export const MARKET_NAMES = { US: "US Market", IN: "Indian Market" };

/** Market to show: explicit route value, else the remembered one, else US. */
export function resolveMarket(routeMarket, stored) {
  for (const m of [routeMarket, stored]) {
    const v = String(m || "").toUpperCase();
    if (v === "US" || v === "IN") return v;
  }
  return "US";
}

export function readStoredMarket(storage = globalThis.localStorage) {
  try {
    return storage?.getItem(MARKET_KEY) || "";
  } catch {
    return "";
  }
}

export function storeMarket(market, storage = globalThis.localStorage) {
  try {
    storage?.setItem(MARKET_KEY, market);
  } catch {
    /* storage unavailable: selection still works through the URL */
  }
}

/** Bars for the sector breakdown chart (counts come from the backend). */
export function sectorBars(overview) {
  return (overview?.sectors || []).map((s) => ({
    key: s.sector_key,
    label: s.label,
    values: [
      { id: "universe", value: s.universe_count, tone: "secondary", title: "Companies in the curated universe" },
      { id: "loaded", value: s.loaded_count, tone: "primary", title: "Companies with data loaded" },
    ],
  }));
}

/** Only rows of the requested market (guards against mixing stale responses). */
export function forMarket(overview, market) {
  return overview && overview.market === market ? overview : null;
}
