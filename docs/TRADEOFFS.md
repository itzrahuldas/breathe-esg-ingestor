# Tradeoffs — Deliberate Cuts

This file documents things the codebase is **aware of but deliberately did not build**,
with the reasoning behind each decision. It is not a bug list — it is a record of
engineering judgment calls.

---

## 1. No role-based authentication

**What it would be:**
Analyst role (can review and approve), Admin role (can configure clients and plant codes),
Read-only role (auditor view, no actions).

**Why not built:**
Adds 2+ days of implementation. The assignment brief defines a single analyst persona.
The product question of who can approve what — and whether approval requires a second
sign-off — is a business decision that belongs to the PM, not the prototype.

The API currently accepts `client_id` as a plain integer query parameter rather than
resolving it from an authenticated session. Authentication (JWT/OAuth) depends on the
identity provider chosen for production (Auth0, Cognito, Keycloak, etc.). The correct
fix is to integrate the real IdP and resolve `client_id` from the verified JWT claim —
at which point all the `client_id` filter calls in `views.py` remain correct; only the
resolution source changes.

**What I would build next:**
Django Groups with three roles, DRF permission classes per endpoint, and a simple role
management screen in the dashboard.

---

## 2. No versioned emission factor pipeline (partial)

**What was built:**
`EmissionFactor` model with `source`, `year`, `effective_from`, `effective_to`, and a
client-override mechanism. 10 global defaults seeded on `GET /api/setup/` (DEFRA 2023,
CEA 2023). `ActivityRow.emission_factor_ref` FK exists and is PROTECT-constrained.

**What was not built:**
Parsers still use hardcoded module-level constants rather than looking up `EmissionFactor`
rows at parse time. The FK `emission_factor_ref` on `ActivityRow` exists but is not
populated during ingestion.

**Why not completed:**
Wiring parsers to do a DB lookup per row adds latency and requires handling the case where
no factor exists for a given key + date combination. For a prototype where all data is from
2023-2024 and all factors are seeded, hardcoded constants produce identical results with
zero complexity.

**What I would build next:**
A `get_emission_factor(client, factor_key, activity_date)` helper that queries
`EmissionFactor` with date range filtering, called from each parser instead of using module
constants.

---

## 3. No PDF bill parsing

**What it would be:**
OCR pipeline for utility PDF bills using `pdfplumber` or AWS Textract, with layout
detection to extract meter ID, consumption, and billing period.

**Why not built:**
OCR is fragile — it breaks when a utility redesigns their bill template. Every utility
has a different PDF layout. Portal CSV is available for all major utilities and produces
cleaner, more reliable data. Approximately 15% of utility clients only have PDF access.

**What I would build next:**
Only after mapping which specific clients need it, and only for utilities that genuinely
have no portal CSV option.

---

## 4. Analyst edit endpoint not built

**What it would be:**
`PATCH /api/rows/{id}/` allowing an analyst to correct `quantity`, `unit`, `scope`, or
`category` before approval.

**Why not built:**
The data model fully supports it — `ActivityRow` has `is_edited`, `edited_at`,
`edited_by_id`, `original_snapshot`, and `emission_factor_ref`. `AuditLog` has
`ACTION_EDITED` defined. The infrastructure is complete.

Editing adds significant validation complexity: should editing a `FLAGGED` row
auto-clear the flag? Should editing `scope` re-run the emission calculation? These
are product decisions that belong to the PM.

**What I would build next:**
`PATCH` endpoint with before/after `AuditLog` entry (`ACTION_EDITED`), automatic
`co2e_kg` recalculation using `EmissionFactor` lookup, and a UI diff view showing
original vs edited values.

---

## 5. Sequential integer PKs instead of UUIDs

**What the problem is:**
All models use Django's `BigAutoField`. Sequential IDs are enumerable — a caller
can iterate `/api/rows/1/`, `/api/rows/2/`, etc.

**Why not changed:**
Migrating PKs to `UUIDField` on an existing schema with multiple FK relationships
requires a coordinated migration across five tables. The correct fix is to apply
UUIDs from the start in a production schema.

**What I would build next:**
`UUIDField(primary_key=True, default=uuid.uuid4, editable=False)` on all models
from day one of a production schema design.

---

## 6. Hardcoded airport coordinates (10 airports)

**What the problem is:**
The travel parser computes flight distances using great-circle haversine against a
hardcoded dict of exactly 10 IATA codes: `BOM, DEL, LHR, BLR, MAA, HYD, CCU,
DXB, SIN, JFK`. Any booking with an unknown origin or destination is flagged.

**Why accepted:**
For the prototype, 10 airports cover the demo dataset. The flagging mechanism ensures
unknown airports are surfaced for human review rather than silently producing a wrong
distance.

**What I would build next:**
Seed `AIRPORT_COORDS` from the OurAirports public domain database (~57 kB CSV).
No API dependency — one-time static data load at startup.
