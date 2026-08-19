(function attachCertificateReviewSummary(global) {
  "use strict";

  const SAFE_COMPARISON_RESULTS = new Set([
    "MATCH", "EQUIVALENT", "CONSISTENT", "FORM_EQUIVALENT", "NOT_COMPARABLE",
  ]);
  const SAFE_MODEL_DECISIONS = new Set(["EQUIVALENT", "CONSISTENT", "PASS"]);

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function activeIssue(issue) {
    const severity = String(issue?.severity || issue?.risk || "UNCERTAIN").toUpperCase();
    return !["INFO", "LOW"].includes(severity);
  }

  function activeModelDecision(decision) {
    const result = String(
      decision?.decision || decision?.result || decision?.verdict || "UNCERTAIN",
    ).toUpperCase();
    const risk = String(decision?.risk || decision?.risk_level || "").toUpperCase();
    return !SAFE_MODEL_DECISIONS.has(result) || ["HIGH", "CRITICAL", "ERROR"].includes(risk);
  }

  function normalizedField(value) {
    return String(value || "").trim().toLowerCase();
  }

  function fieldConcernKey(value) {
    const field = normalizedField(value);
    return field ? `field:${field}` : "";
  }

  function issueConcernKey(issue) {
    const fieldKey = fieldConcernKey(issue?.field || issue?.field_name);
    if (fieldKey) return fieldKey;
    const code = String(issue?.code || "review_issue").trim().toUpperCase();
    if (code.startsWith("AUTHENTICITY_")) return "authenticity";
    if (code === "FROZEN_LEDGER_MISSING") return "frozen-ledger";
    return `issue:${code}`;
  }

  function modelConcernKeys(decision) {
    const fields = new Set();
    const direct = fieldConcernKey(decision?.field || decision?.field_name);
    if (direct) fields.add(direct);
    asArray(decision?.affected_fields).forEach((field) => {
      const key = fieldConcernKey(field);
      if (key) fields.add(key);
    });
    const evidence = decision?.evidence || {};
    asArray(evidence.differences).forEach((item) => {
      const key = fieldConcernKey(item?.field || item?.field_name);
      if (key) fields.add(key);
    });
    asArray(evidence.issues).forEach((item) => {
      const key = fieldConcernKey(item?.field || item?.field_name);
      if (key) fields.add(key);
    });
    return fields.size ? [...fields] : ["model-review"];
  }

  function authenticityConcern(detail) {
    const certificate = detail?.certificate || {};
    const status = String(
      detail?.authenticity?.status
      || certificate.authenticity?.status
      || detail?.authenticity_status
      || certificate.authenticity_status
      || "",
    ).toUpperCase();
    return Boolean(status) && status !== "VERIFIED";
  }

  function countActiveConcerns(detail) {
    const comparisons = asArray(detail?.comparisons);
    const issues = asArray(detail?.issues);
    const decisions = asArray(detail?.model_decisions);
    const concerns = new Set();
    comparisons.forEach((item) => {
      const result = String(item?.status || item?.result || item?.decision || "UNCERTAIN").toUpperCase();
      if (!SAFE_COMPARISON_RESULTS.has(result)) {
        concerns.add(fieldConcernKey(item?.field || item?.field_name) || `comparison:${result}`);
      }
    });
    issues.filter(activeIssue).forEach((issue) => concerns.add(issueConcernKey(issue)));
    decisions.filter(activeModelDecision).forEach((decision) => {
      modelConcernKeys(decision).forEach((key) => concerns.add(key));
    });
    if (authenticityConcern(detail)) concerns.add("authenticity");
    return concerns.size;
  }

  const api = { countActiveConcerns };
  global.CertificateReviewSummary = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
}(typeof globalThis !== "undefined" ? globalThis : this));
