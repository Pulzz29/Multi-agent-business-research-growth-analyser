// Saved Reports filtering (pure; unit-tested). The backend already sorts newest first.

export function filterSavedReports(reports, market = "ALL", company = "ALL") {
  return (reports || []).filter(
    (r) => (market === "ALL" || r.market === market) && (company === "ALL" || r.ticker === company)
  );
}
