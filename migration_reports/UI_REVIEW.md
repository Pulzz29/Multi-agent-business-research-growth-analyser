# UI review

Tested in the Claude desktop app's built-in browser against the Vite dev server (`http://127.0.0.1:5173`), with requests proxied to the local backend.

## Verified

| Check | Result |
|---|---|
| App loads, title "InsightIQ — Business Research" | PASS |
| Sidebar navigation: Overview, Research, Activity | PASS, all three pages render |
| Signed-out state | "Connect your workspace" card with a password-type key field, Show toggle and Connect button |
| Invalid access key | Rejected with "The access key was not accepted." (backend 401, shown inline) |
| Research page | Company selector with all six companies (Apple, Amazon, Alphabet, Meta, Microsoft, NVIDIA), "Company news" panel with the **Refresh MSFT news** button and its note, "Start research" control, "Research progress" panel |
| Activity page | Renders its signed-out state ("Connect to see your research performance") |
| Dark → light → dark theme | Switches correctly; stored as `insightiq-theme` in localStorage |
| Credential handling | The access key is **not** stored in localStorage or sessionStorage (only the theme key exists); key field is `type=password` |
| Mobile (375×812) | Layout collapses to a hamburger menu; no horizontal overflow (`scrollWidth` = 375) |
| Keyboard | Tab moves through header and form controls; focused button shows a 2.4 px solid outline |
| Accessibility basics | `lang="en"`; every button has text or an aria-label; no images without alt |
| Console | Only the expected `401` from the invalid-key attempt; no JavaScript errors |
| Proxy | `/health/live` → 200 and `/api/v1/companies` (no key) → 401 through port 5173 |
| Production build | `npm run build` succeeds; output byte-identical to the Colab build |

## Signed-in verification (2026-11-23)

Done with a throwaway access key in a temporary `.env.uitest` profile. The file was deleted afterwards and the backend restarted, after which the key returns 401. Your `.env` was not read.

| View | Result |
|---|---|
| Overview | 6 companies · 12,737 financial facts · 75 news articles · 5 completed reports (6 total jobs) · recent list of 6 MSFT runs (5 Completed, 1 Failed) · System status: Backups **Verified**, Reliability "Insufficient Data", workflow p95 7.41 s |
| Saved report (run 5c401240) | Opens from the recent list; agent steps Financial/News/Strategic all Completed; **Financial** tab: 4 source-backed findings, "View evidence" expands to SEC evidence (concept, period, value); **News** tab: 2 selected articles with evidence; **Strategy** tab: 3 priorities (high/medium/low) with evidence |
| Research / Refresh News | MSFT: 23 saved, latest published Nov 22 1:30 PM, last refresh Nov 23 1:47 AM (IST). Switching to Apple updates the panel (4 saved). The **Refresh** and **Start research** buttons are present and enabled and were deliberately **not clicked**. |
| Activity | 8 recorded spans, workflow p95 7.41 s, 5 verified backups, 6 runs; per-agent p95; reliability panel; data protection: 5 completed jobs checked, 0 unverified |
| Console | No JavaScript errors; only the expected 401 from the signed-out load |

## Observations (not changed; existing behaviour)

- The page has no `<h1>`; headings start lower. This is a minor accessibility nit.
- Vite binds to `127.0.0.1`. Use `http://127.0.0.1:5173`; `http://localhost:5173` also works in most browsers via IPv4 fallback.
