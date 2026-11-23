# Migration plan (as executed)

| # | Step | Rationale | Outcome |
|---|---|---|---|
| 1 | Analyse all 136 notebook cells; offline, network-blocked replay | Understand the build sequence and Colab dependencies without live calls | Found that two modules and all data exist only on Drive (see [NOTEBOOK_ANALYSIS.md](NOTEBOOK_ANALYSIS.md)) |
| 2 | Audit the local environment | Choose matching Python and Node | Python 3.13 venv with Colab-recorded pins; Node 24 ([ENVIRONMENT_AUDIT.md](ENVIRONMENT_AUDIT.md)) |
| 3 | Request the Drive build export | Only authoritative copy of the final source and data | Received; ZIP CRC-verified, 243 entries |
| 4 | Back up: ZIP copy + read-only extraction + per-file SHA-256 | Recoverability; prove nothing was altered | Done; re-verified by tests |
| 5 | Read-only data baseline | Record integrity and counts before any change | [DATABASE_PRESERVATION.md](DATABASE_PRESERVATION.md) |
| 6 | Install the export into `Documents\InsightIQ` | Separate from the older `Multi-Agent-Business-Research` project | Done; old project untouched |
| 7 | Minimal compatibility fixes (lock, connection close, legacy path rebasing) | Backend could not import or start on Windows; reports returned 503 | 6 files / 58 lines ([MIGRATION_CHANGELOG.md](MIGRATION_CHANGELOG.md)) |
| 8 | Local runtime preparation with the app's own restore and backup functions | Avoid restoring the stale 69-article backup; satisfy the source-identity check | Runtime DB seeded from the 75-article snapshot |
| 9 | `.env`/`.env.example`/`.env.test`, `.gitignore`, VS Code configs, start scripts | Secure local configuration and one-command start | Done |
| 10 | `npm ci` + build; Vite proxy check | Frontend parity | Byte-identical bundle; proxy OK |
| 11 | Start both services; browser checks | Real runtime verification | Running; signed-out UI verified |
| 12 | 114-test offline suite; restart test | Non-destructive verification | All pass ([TEST_RESULTS.md](TEST_RESULTS.md)) |
| 13 | Reports | Documentation | This folder |

## Pending your approval

- One live NewsData.io refresh (uses quota).
- One Groq research job (uses quota).
- Signed-in UI check, which needs you to paste the access key.
