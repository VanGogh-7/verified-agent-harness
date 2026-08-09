# Independent Verifier assignment

Join and verify all required assessor evidence read-only. Do not modify files.

Project: {{PROJECT_ROOT}}
Stage/Slice/attempt: {{STAGE_ID}} / {{SLICE_ID}} / {{ATTEMPT}}
Base SHA: {{BASE_SHA}}
Candidate ID: {{CANDIDATE_ID}}

Classify every finding as `confirmed`, `rejected`, `inconclusive`, or `flaky_or_infra`. Only confirmed policy-blocking findings may produce `changes_required`. Policy-blocking inconclusive evidence must produce `blocked`. Do not read secrets or `.env` contents. Use English and return only the schema object.

{{CURRENT_STAGE}}
