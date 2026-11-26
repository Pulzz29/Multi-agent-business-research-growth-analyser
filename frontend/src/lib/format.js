// Formatting helpers. Missing values are always shown as unavailable, never as zero.

export const UNAVAILABLE = "—";

export function toNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

export function formatUsd(value, { digits = 2 } = {}) {
  const n = toNumber(value);
  if (n === null) return UNAVAILABLE;
  const abs = Math.abs(n);
  const sign = n < 0 ? "−" : "";
  if (abs >= 1e12) return `${sign}$${(abs / 1e12).toFixed(digits)}T`;
  if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(digits)}B`;
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(1)}M`;
  return `${sign}$${abs.toLocaleString("en-US")}`;
}

export function formatUsdLong(value) {
  const n = toNumber(value);
  if (n === null) return UNAVAILABLE;
  const abs = Math.abs(n);
  const sign = n < 0 ? "−" : "";
  if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(2)} billion`;
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(1)} million`;
  return `${sign}$${abs.toLocaleString("en-US")}`;
}

/** Exact value with unit, for title/hover text. */
export function exactUsd(value, unit = "USD") {
  if (value === null || value === undefined || value === "") return "Unavailable";
  const text = String(value);
  const [whole, fraction] = text.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${unit} ${grouped}${fraction ? `.${fraction}` : ""}`;
}

export function formatBillions(value, digits = 2) {
  const n = toNumber(value);
  if (n === null) return UNAVAILABLE;
  return `$${n.toFixed(digits)}B`;
}

export function formatPct(value, digits = 1) {
  const n = toNumber(value);
  if (n === null) return UNAVAILABLE;
  const sign = n > 0 ? "+" : n < 0 ? "−" : "";
  return `${sign}${Math.abs(n).toFixed(digits)}%`;
}

export function trendTone(value) {
  const n = toNumber(value);
  if (n === null) return "neutral";
  return n > 0 ? "positive" : n < 0 ? "negative" : "neutral";
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function parseIsoDate(value) {
  if (!value) return null;
  const text = String(value);
  // Date-only strings are calendar dates; avoid timezone shifts.
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
    const [y, m, d] = text.split("-").map(Number);
    return { y, m, d, dateOnly: true };
  }
  const parsed = new Date(text);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatDate(value) {
  const parsed = parseIsoDate(value);
  if (!parsed) return UNAVAILABLE;
  if (parsed.dateOnly) return `${MONTHS[parsed.m - 1]} ${parsed.d}, ${parsed.y}`;
  return parsed.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function formatDateTime(value) {
  const parsed = parseIsoDate(value);
  if (!parsed) return UNAVAILABLE;
  if (parsed.dateOnly) return formatDate(value);
  return parsed.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function relativeTime(value) {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  const seconds = Math.round((Date.now() - parsed.getTime()) / 1000);
  if (seconds < 0) return "just now";
  if (seconds < 90) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 36) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 60) return `${days} days ago`;
  const months = Math.round(days / 30);
  return `${months} months ago`;
}

/** "Oct–Dec 2025" for a standalone quarter. */
export function periodRange(start, end) {
  const s = parseIsoDate(start);
  const e = parseIsoDate(end);
  if (!s?.dateOnly || !e?.dateOnly) return end ? formatDate(end) : UNAVAILABLE;
  const startMonth = MONTHS[s.m - 1];
  const endMonth = MONTHS[e.m - 1];
  return s.y === e.y ? `${startMonth}–${endMonth} ${e.y}` : `${startMonth} ${s.y}–${endMonth} ${e.y}`;
}

export function shortPeriod(end) {
  const e = parseIsoDate(end);
  if (!e?.dateOnly) return UNAVAILABLE;
  return `${MONTHS[e.m - 1]} ’${String(e.y).slice(2)}`;
}

export function formatSeconds(value) {
  const n = toNumber(value);
  if (n === null) return UNAVAILABLE;
  return n < 10 ? `${n.toFixed(2)} s` : `${n.toFixed(1)} s`;
}

export function formatCount(value) {
  const n = toNumber(value);
  return n === null ? UNAVAILABLE : n.toLocaleString("en-US");
}

export function shortId(id) {
  return id ? String(id).slice(0, 8) : UNAVAILABLE;
}

export function humanize(value) {
  if (!value) return UNAVAILABLE;
  return String(value)
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function duration(start, end) {
  if (!start || !end) return null;
  const ms = new Date(end).getTime() - new Date(start).getTime();
  return Number.isFinite(ms) && ms >= 0 ? ms / 1000 : null;
}

const CURRENCY_SYMBOLS = { USD: "$", INR: "₹", EUR: "€", GBP: "£", JPY: "¥" };

/**
 * Compact money in the company's own currency (never converted).
 * INR uses Indian units: crore (1e7) and lakh crore (1e12); USD uses M/B/T.
 */
export function formatMoney(value, currency = "USD", { digits = 2 } = {}) {
  const n = toNumber(value);
  if (n === null) return "—";
  const sign = n < 0 ? "-" : "";
  const a = Math.abs(n);
  const sym = CURRENCY_SYMBOLS[currency] ?? (currency ? `${currency} ` : "");
  if (currency === "INR") {
    if (a >= 1e12) return `${sign}${sym}${(a / 1e12).toFixed(digits)} L Cr`;
    if (a >= 1e7) return `${sign}${sym}${(a / 1e7).toLocaleString("en-IN", { maximumFractionDigits: digits })} Cr`;
    return `${sign}${sym}${a.toLocaleString("en-IN", { maximumFractionDigits: digits })}`;
  }
  if (a >= 1e12) return `${sign}${sym}${(a / 1e12).toFixed(digits)}T`;
  if (a >= 1e9) return `${sign}${sym}${(a / 1e9).toFixed(digits)}B`;
  if (a >= 1e6) return `${sign}${sym}${(a / 1e6).toFixed(digits)}M`;
  return `${sign}${sym}${a.toLocaleString("en-US", { maximumFractionDigits: digits })}`;
}

/** Per-share amounts (EPS, price): full value with the currency symbol. */
export function formatPerShare(value, currency = "USD") {
  const n = toNumber(value);
  if (n === null) return "—";
  const sym = CURRENCY_SYMBOLS[currency] ?? (currency ? `${currency} ` : "");
  return `${n < 0 ? "-" : ""}${sym}${Math.abs(n).toLocaleString(currency === "INR" ? "en-IN" : "en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** Chart axis divisor and unit label for a currency (crore for INR, billions otherwise). */
export function moneyScale(currency) {
  return currency === "INR" ? { divisor: 1e7, unit: "₹ crore" } : { divisor: 1e9, unit: `${CURRENCY_SYMBOLS[currency] || currency || ""} billions`.trim() };
}

/**
 * Fiscal period label. India: FY runs April-March, named by the ending year
 * (2025-03-31 -> FY2025; Q1 = Apr-Jun). Other markets: the period end.
 */
export function fiscalLabel(periodEnd, market, periodType = "annual") {
  const m = /^(\d{4})-(\d{2})-\d{2}/.exec(String(periodEnd || ""));
  if (!m) return String(periodEnd || "");
  const year = Number(m[1]);
  const month = Number(m[2]);
  if (market === "IN") {
    const fy = month <= 3 ? year : year + 1;
    if (periodType === "quarterly") return `Q${month <= 3 ? 4 : month <= 6 ? 1 : month <= 9 ? 2 : 3} FY${fy}`;
    return `FY${fy}`;
  }
  return periodType === "quarterly" ? `Qtr ending ${periodEnd.slice(0, 10)}` : `FY ending ${periodEnd.slice(0, 10)}`;
}
