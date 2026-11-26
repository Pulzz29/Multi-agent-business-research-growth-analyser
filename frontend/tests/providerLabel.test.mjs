// AI provider labels come from each saved report's own metadata (node --test).

import assert from "node:assert/strict";
import test from "node:test";

import { aiProviderLabel, COMPANIES, companyByTicker, setSecCompanies, shortCompanyName } from "../src/lib/reportModel.js";

test("Azure reports are labelled with the served model", () => {
  assert.equal(
    aiProviderLabel({ provider: "azure_openai", deployment: "test-model", model: "test-model-2026-01-01" }),
    "Azure OpenAI · test-model-2026-01-01"
  );
  assert.equal(aiProviderLabel({ provider: "azure_openai", deployment: "my-deploy", model: null }), "Azure OpenAI · my-deploy");
});

test("historical Groq reports are never relabelled as Azure", () => {
  assert.equal(aiProviderLabel({ model: "legacy/test-model" }), "Groq · legacy/test-model");
  assert.equal(aiProviderLabel(null), null);
});

test("SEC company list comes from the backend and keeps fallbacks honest", () => {
  const before = COMPANIES.slice();
  setSecCompanies([]); // an empty response never wipes the list
  assert.deepEqual(COMPANIES, before);
  setSecCompanies([{ ticker: "ORCL", name: "Oracle Corporation" }, { ticker: "MSFT", name: "Microsoft Corporation" }]);
  assert.equal(companyByTicker("ORCL").name, "Oracle");
  assert.equal(companyByTicker("AAPL"), undefined);
  assert.equal(shortCompanyName("The Walt Disney Company"), "Walt Disney");
  setSecCompanies(before.map((c) => ({ ticker: c.ticker, name: c.legal })));
});
