// Display helpers for company data freshness. The refresh DECISION is made by
// the backend (POST /markets/{m}/companies/{symbol}/data); nothing here decides
// whether to fetch.

const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export function relativeAge(iso, now = new Date()) {
  const then = new Date(iso);
  if (!iso || Number.isNaN(then.getTime())) return null;
  const diff = Math.max(0, now.getTime() - then.getTime());
  if (diff < MINUTE) return "just now";
  if (diff < HOUR) {
    const m = Math.floor(diff / MINUTE);
    return `${m} minute${m === 1 ? "" : "s"} ago`;
  }
  if (diff < DAY) {
    const h = Math.floor(diff / HOUR);
    return `${h} hour${h === 1 ? "" : "s"} ago`;
  }
  const d = Math.floor(diff / DAY);
  return `${d} day${d === 1 ? "" : "s"} ago`;
}

/** "Last updated: 3 hours ago (23 Nov 2026, 14:05)" in the viewer's local time. */
export function lastUpdatedLabel(iso, now = new Date(), locale = undefined, timeZone = undefined) {
  const relative = relativeAge(iso, now);
  if (!relative) return "Last updated: never";
  const exact = new Date(iso).toLocaleString(locale, {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    ...(timeZone ? { timeZone } : {}),
  });
  return `Last updated: ${relative} (${exact})`;
}

/**
 * One automatic data request per opened company. Returns a function that is
 * true the first time it sees a key and false afterwards, so React StrictMode
 * double effects and re-renders do not send duplicate requests.
 */
export function createOpenGuard() {
  let lastKey = null;
  return (key) => {
    if (key === lastKey) return false;
    lastKey = key;
    return true;
  };
}
