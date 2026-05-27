# Trade-offs and Deliberate Scope Decisions

This file documents things the codebase is **aware of but deliberately did not build**,
with the reasoning behind each decision. It is not a bug list — it is a record of
engineering judgment calls.

---

## Analyst edit endpoint not built

A `PATCH /api/rows/{id}/` endpoint that lets analysts correct `quantity`, `unit`,
`scope`, or `category` before approval was not built.

**The data model fully supports it:**

- `ActivityRow` has `is_edited`, `edited_by_id`, `edited_at`, `original_snapshot`
  (populated automatically by the `save()` override — no additional migration needed)
- `AuditLog` has `ACTION_EDITED` defined and ready
- The audit infrastructure is complete end-to-end

**Reason not built:** The assignment prioritises the ingestion → review → approve →
lock lifecycle. An edit endpoint adds significant validation complexity:

- Should editing a `FLAGGED` row auto-clear the flag? Or should it require a
  separate flag-clear action?
- Should editing a `REJECTED` row reset it to `PENDING` for re-review?
- Should editing `quantity` trigger recalculation of `co2e_kg`?
- Who is authorised to edit — any user or only reviewers?

These decisions belong to the product manager, not the prototype. The infrastructure
is in place to wire this up quickly once the policy is defined.

---

## No authentication / authorisation layer

The API accepts `client_id` as a plain integer query parameter rather than resolving
it from an authenticated session. Any caller who knows (or guesses) a `client_id` can
read that tenant's data.

**Reason not built:** Authentication (JWT/OAuth) is a platform-level concern that
depends on the identity provider chosen for the production deployment (Auth0, Cognito,
Keycloak, etc.). Adding a stub auth layer would create false confidence. The correct
fix is to integrate the real IdP and resolve `client_id` from the verified JWT claim
— at which point all the `client_id` filter calls in `views.py` remain correct; only
the resolution source changes.

---

## Sequential integer PKs instead of UUIDs

All models use Django's `BigAutoField` (auto-incrementing 64-bit integer) as the
primary key. Sequential IDs are enumerable — a caller can iterate `/api/rows/1/`,
`/api/rows/2/`, etc.

**Reason not changed:** Migrating PKs to `UUIDField` on an existing schema with
multiple FK relationships requires a coordinated migration across five tables. The
risk of data loss during migration outweighs the benefit for a prototype. The correct
fix is to apply UUIDs from the start in a production schema, or perform the migration
with a careful data-backfill script.

---

## Hardcoded emission factors in parser constants

Emission factors (`DIESEL_LITRES = 2.68`, `INDIA_GRID_KWH = 0.716`, etc.) are
module-level constants in each parser file rather than database rows.

**Reason accepted for now:** The `EmissionFactor` model and `emission_factor_ref` FK
on `ActivityRow` are now in place (added in Step 2). The parsers do not yet look up
factors from this table — they still use the constants. Wiring the parsers to the
database requires decisions about factor selection logic (by country? by year? by
client override priority?) that should be validated with the client before building.

---

## Hardcoded airport coordinates (10 airports)

The travel parser computes flight distances using great-circle haversine against a
hardcoded dict of 10 IATA codes. Any booking with an unknown origin or destination
is flagged rather than computed.

**Reason accepted:** A comprehensive airport database (e.g. OurAirports CSV with
~9,000 airports) would be correct but introduces a data dependency and seeding step.
For the prototype, 10 airports cover the demo dataset. The flagging mechanism ensures
unknown airports are surfaced for human review rather than silently producing a wrong
distance.
