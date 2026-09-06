# Client onboarding ↔ future ZoomInfo boundary

This document defines what the onboarding wizard prepares for later ZoomInfo
company/contact searches. It does **not** authorize any ZoomInfo API work.

## In scope for onboarding (now)

- Structured client capability and target-profile text fields
- Ideal customer types, industries, geography, titles, and fit signals
- Campaign criteria used by Research / Fit
- Status catalog labels for CRM workflow

## Explicitly out of scope (now)

- ZoomInfo API credentials or connection UI
- Live company/contact search, enrichment, or import
- Storing ZoomInfo account tokens in drafts or client profiles
- Automatic matching of prospects against ZoomInfo

## Future mapping boundary (later work)

When ZoomInfo is connected under a separate, reviewed change:

1. Map **target industries / customer types / geography / titles** from the
   completed client profile into ZoomInfo search filters.
2. Map **positive fit signals** only as soft ranking hints — never as invented
   capabilities.
3. Keep credentials in the existing secure integration store (not Client Setup
   forms, not onboarding drafts, not frontend env).
4. Never write ZoomInfo results into CRM masters without an explicit import
   confirmation path (same discipline as CRM import).

Templates and copy-from-client flows must continue to exclude credentials,
email connections, users, assignments, prospects, and research results.
