---
name: northstar-code-review
description: Review the NorthStar AI codebase for confirmed defects, security risks, client-scoping leaks, data-integrity problems, unsafe database behavior, API/frontend mismatches, and missing tests. Use when asked to review, audit, inspect, verify, find problems, or check a NorthStar change before approval.
---

# NorthStar Code Review

Review NorthStar AI carefully and remain read-only by default.

Do not modify application files, database records, configuration, dependencies, or running processes unless the user separately approves fixes.

Do not restart or stop Uvicorn, Vite, or another process during the review.

## Review priorities

1. Check client isolation.

- Carmeco and Brown Industries data must not become mixed.
- Writes must target an explicit active client.
- All My Clients may provide authorized cross-client reads, but must not cause ambiguous writes.
- Shared master companies and contacts must retain client-specific status, workflow, campaign membership, tasks, notes, and activity.

2. Check database safety.

- Multi-step operations must use atomic transactions.
- A failure must not leave partial company, contact, campaign, task, appointment, or opportunity changes.
- Imports must validate, deduplicate, report errors, and support rollback.
- Tests must not modify the production NorthStar database.
- Flag missing migrations or backup requirements.

3. Check security.

- Never expose or commit API keys, OAuth credentials, tokens, passwords, or encryption keys.
- Flag secrets in source files, logs, API responses, screenshots, or test fixtures.
- Verify authorization and client access on backend endpoints.
- Validate and sanitize external upload and API data.

4. Check integrations and imports.

- Review spreadsheet uploads and future ZoomInfo imports for validation, matching, deduplication, provenance, retry safety, and audit history.
- External records must not silently overwrite user-entered NorthStar information.
- Repeated imports must not create duplicate companies, contacts, relationships, activities, or campaign assignments.

5. Check backend and frontend consistency.

- Confirm frontend request and response types match FastAPI endpoints.
- Review routing, return links, loading states, errors, empty states, and active-client changes.
- The current backend port is 8007. Treat port 8006 as stale unless the user explicitly changes this.
- Flag hard-coded ports, stale proxy settings, or conflicting configuration.

6. Check performance.

- Review queries for pagination, indexes, N+1 behavior, and unsafe unbounded results.
- Assume approximately 264,000 companies and 367,000 contacts.
- Flag operations that load entire large tables into memory.

7. Check verification.

- Run only safe, read-only checks.
- Run relevant backend tests and `npm run build` when available.
- Do not claim a test passed unless it was actually executed.
- Separate confirmed defects from risks, performance concerns, and suggestions.

## Evidence requirements

Every finding must include:

- Severity: Critical, High, Medium, or Low
- Classification: Confirmed defect, Risk, Performance concern, or Suggestion
- File path and line number when available
- Evidence showing why it is a problem
- Expected effect on users or data
- A concise recommended correction

Do not invent findings to make the review appear useful.

If no confirmed defect is found, say so and identify anything that could not be verified.

## Required report format

1. Verdict
2. Findings by severity
3. Tests and checks run
4. What was not verified
5. Recommended fix order

Finish the review without making changes. Ask for approval before implementing any recommended correction.
