# Proposal: richer explanations from the Financial, News and Strategic agents

**Status:** proposal only. Nothing in this document has been applied. No agent prompt, schema or validator was changed, and no live call was made for it.

## Why

Saved reports are accurate but thin. The dashboard now renders everything they contain; the shortness comes from the agents' output, not from the UI.

| Agent | What it returns today | Prose written by the model |
|---|---|---|
| Financial | 4 selected SEC claims. The statements are built from a template (`financial_claims.py:178`). | None |
| News | Up to 4 selected article IDs plus one topic tag. Headline and description come from NewsData. | None |
| Strategic | 2–4 evidence IDs, each with a theme and a priority. `observed_evidence` copies the source text, and `monitoring_question` is a fixed template (`strategic_agent.py:40-69`). | None |

The agents were deliberately designed as *selectors* to remove the risk of fabrication. The proposal keeps that design and adds a small, separately validated *explanation* field to each selection.

## Proposed changes (additive; existing fields unchanged)

### Financial Agent (`services/financial_claims.py`)
- **Input:** add InsightIQ-computed comparisons for each candidate observation (previous quarter, same quarter a year earlier, % change). They come from the same code the dashboard uses (`company_profile.build_series`).
- **Output, per claim:**
  - `interpretation`: 2–4 sentences on what the observation shows and how it compares.
  - `uncertainty`: 1 sentence.
- **Validation:** reuse `executive_summary.validate_summary`'s checks:
  - Every number must appear in the supplied input.
  - Causal, predictive and advisory phrases are rejected.
  - At most 90 words per claim.
- **Tokens:** completion 1500 → about 2200; input +≈400.

### News Agent (`agents/news_agent.py`)
- **Output, per selection:**
  - `reported_development`: 1–2 sentences attributing the development to the publisher ("X reports that…").
  - `potential_relevance`: 1–2 sentences, framed as something to verify, never as an impact.
- **Validation:**
  - Numbers must come from that article's own title or description.
  - Banned causal and impact phrases are rejected.
  - Headline, URL, publisher and date are still copied from the database.
- **413 impact:** completion 1200 → about 1800. Inside today's 6000-token request budget, the input allowance falls from about 4800 to about 4200, so roughly one fewer article would be sent per request. Alternatively, set `INSIGHTIQ_NEWS_REQUEST_TOKEN_BUDGET=7000` (allowed range 1700–131072). `fit_articles_to_budget` still never truncates an article.

### Strategic Agent (`agents/strategic_agent.py`)
- **Output, per priority:**
  - `why_it_matters`: 2–3 sentences grounded only in the cited evidence.
  - `what_to_watch`: 1–3 short questions.
- The fixed `monitoring_question` stays as a fallback.
- **Validation:**
  - Same number and phrase checks as above.
  - The theme must still equal the evidence's `allowed_theme`.
  - `financial_impact_claimed` and `causal_relationship_claimed` stay `false` and are enforced.
- **Tokens:** completion 1200 → about 1700.

## Compatibility

- All new fields are optional in the saved report. The 9 existing reports render unchanged.
- The report viewer would show the new fields when present: "Interpretation", "What the article reports / Potential relevance" and "Why it matters".
- The job status model, LangGraph graph shape, evidence IDs and the backup and recovery flow are unchanged.

## Cost and risk

- About 1.5k more completion tokens per research run across the three agents.
- The main risk is more validation rejections, where a whole run fails because of one ungrounded sentence. To avoid that, a rejected explanation would be dropped (the selection is kept) rather than failing the agent.

## Rollout (each step needs your approval)

1. Implement the changes behind `INSIGHTIQ_AGENT_EXPLANATIONS=1`, with tests that mock Groq.
2. Run one live research job for one company, and review the report together.
3. Enable by default if you are satisfied.
