# Independent Tester assignment

Test the current candidate read-only. Do not modify files.

Project: {{PROJECT_ROOT}}
Stage/Slice/attempt: {{STAGE_ID}} / {{SLICE_ID}} / {{ATTEMPT}}
Base SHA: {{BASE_SHA}}
Candidate ID: {{CANDIDATE_ID}}

Run only safe, in-scope checks. Use `passed` when required testing succeeds. Use `failed` for a product failure and include at least one critical/high finding with `blocking_recommendation=true`; those findings are hypotheses for the Verifier. Use `flaky_or_infra` only when infrastructure or nondeterminism prevents a trustworthy product result, and describe it in `limitations`; that outcome blocks before Verifier and does not request product repair. Do not read secrets or `.env` contents. Use English and return only the schema object.

{{CURRENT_STAGE}}
