# AdminPilot — Architecture (v1.0)

**Status:** Authoritative. Covers the tenancy model, technology stack, and registration/access architecture for this build.

**Stack note:** This project was scoped by adapting an earlier planning document that used a different stack (Postgres/Supabase/Next.js). That earlier stack is **not used anywhere in this build** and shouldn't appear in any code, config, or dependency. This project is Django + MSSQL only. This note exists once, here, so it never needs repeating below.

---

## ADR-001 — Multi-Tenant Architecture

**Status:** Approved

**Decision:** AdminPilot is a multi-tenant SaaS platform from the start. Every institution has a fully isolated workspace; every major record (Institution, Users, Students, Classes, Sessions, Terms, Fee Structures, Payments, Receipts, Reports, Audit Logs) belongs to exactly one Institution. No institution is a special case in the schema, even though the pilot cohort is small (2–5 schools expected in year one).

**Data/owner independence — the concrete guarantee:** An Owner, Administrator, or Bursar at Institution A has zero visibility into Institution B's existence — not its students, payments, staff accounts, or even the fact that it's a customer. This is a database-enforced guarantee, not a UI-hiding one.

**Why:** Scalability without redesign, single codebase, straightforward onboarding, a strong security boundary, and commercial readiness for a subscription SaaS model.

**Implementation:**

SQL Server's **Row-Level Security** feature provides this at the database level: Security Policies built from predicate functions, trusted at the same level as "the database refuses the query on its own even if application code has a bug":

```sql
-- Predicate function: returns a row only if it belongs to the session's current institution
CREATE FUNCTION dbo.fn_TenantAccessPredicate(@institution_id INT)
RETURNS TABLE
WITH SCHEMABINDING
AS
RETURN SELECT 1 AS fn_result
WHERE @institution_id = CAST(SESSION_CONTEXT(N'institution_id') AS INT);

-- Security policy applied per tenant-scoped table
CREATE SECURITY POLICY dbo.TenantIsolationPolicy
ADD FILTER PREDICATE dbo.fn_TenantAccessPredicate(institution_id) ON dbo.students,
ADD BLOCK PREDICATE dbo.fn_TenantAccessPredicate(institution_id) ON dbo.students AFTER INSERT,
ADD BLOCK PREDICATE dbo.fn_TenantAccessPredicate(institution_id) ON dbo.students AFTER UPDATE
WITH (STATE = ON);
```

`FILTER PREDICATE` blocks cross-tenant reads. `BLOCK PREDICATE ... AFTER INSERT/UPDATE` blocks cross-tenant writes — a user cannot insert or update a row into another institution's `institution_id` either, not just fail to see it afterward. This policy is repeated per tenant-scoped table (students, classes, fee_structures, payments, receipts, audit_logs, etc.) — see `02_Database.md` for the full table list.

**The session-context handoff (the part most likely to be gotten wrong):** `SESSION_CONTEXT` is tied to the physical database connection, and Django's connection pooling reuses connections across unrelated requests. This means `EXEC sp_set_session_context @key = N'institution_id', @value = @current_user_institution;` must run **at the start of every request**, inside Django middleware, using the authenticated user's institution — never assumed to persist from a prior request on the same pooled connection. This is the single highest-risk implementation detail in the whole platform; get the Phase 1 verification checklist right before building anything on top of it (see `02_Database.md`).

**Second layer (defense in depth, not the floor):** Every tenant-scoped Django model inherits from a `TenantScopedModel` abstract base with a custom manager that automatically filters by the current institution (resolved from request context). This exists so application code *looks* correct and fails safely if something upstream is misconfigured — but the Security Policy above is the actual guarantee. **RLS is the enforcement mechanism, not a backstop.**

**Accepted constraint:** one user account belongs to exactly one institution in V1. No multi-campus-under-one-owner. Reopening this is a real architectural change — route through a CR, don't retrofit casually.

---

## ADR-002 — Technology Stack

**Status:** Approved

**Decision:**

| Layer | Choice |
|---|---|
| Backend + ORM | Django |
| Database | Microsoft SQL Server, via `mssql-django` (Microsoft's maintained backend) |
| Auth | Django's built-in auth system, extended with `institution_id`/role fields — no external auth provider |
| View layer | Plain Django views — no DRF, no separate JS framework. See "View Layer Pattern" below. |
| File storage | TBD — no file/photo/document upload is in V1 scope yet beyond student passport photo (see `05_Scope_Boundary.md`); revisit when that's built |

**Why MSSQL:** fixed requirement for this build — existing familiarity/infrastructure. Tenant isolation is still fully achievable at the database level via SQL Server's native Row-Level Security (ADR-001, above); this was confirmed before committing to the choice.

### View Layer Pattern

**Decision:** Plain Django views — function-based and class-based generic views (`ListView`, `DetailView`, `CreateView`, `UpdateView`, `DeleteView`) — Django templates, and Django Forms. No Django REST Framework, no React/Vue. Frontend interactivity is vanilla JS, with a small number of hand-rolled endpoints returning `JsonResponse` where a page genuinely needs structured data without a full reload — in practice this is just global search-as-you-type (see `03_Views_and_Endpoints.md`). Bulk import, despite involving a multi-step upload/validate/preview flow, turns out not to need this — a plain multi-step form flow (upload → server-rendered preview page → confirm) covers it without inventing a JSON contract nothing else uses. This is not a REST API layer; it should not accumulate DRF-style conventions (serializer classes, viewsets, routers) by habit.

**Why not DRF:** DRF earns its complexity when a separate frontend (SPA or mobile) consumes the backend as a real API surface. That's explicitly not the plan here — vanilla JS, no React/Vue, no mobile app planned. Using DRF anyway would mean maintaining a serializer layer that nothing but this same app's own JS ever calls, for no real benefit.

**Bursar field-restriction requirement:** a restricted `ModelForm` (fewer fields in `Meta.fields`) for Bursar-role requests, and a role-aware `get_queryset()`/context builder for read views, keep Bursar's limited view of student data enforced at the view layer, not just hidden in the template.

**`TenantScopedQuerysetMixin`** — every CBV touching a tenant-scoped model inherits this mixin, which filters `get_queryset()` to the current institution (resolved from the authenticated session). This is the view-layer counterpart to the `TenantScopedModel` custom manager in `02_Database.md` — same defense-in-depth principle: the database Security Policy is the real floor, this mixin is a second layer.

### Trade-offs to watch

- SQL Server Row-Level Security is a less common pattern in the wild for multi-tenant SaaS specifically than it is for its more typical use cases — less community tooling, more manual verification burden. The Phase 1 verification checklist (`02_Database.md`) is not optional busywork; it's covering ground a mature library would otherwise cover.
- No managed auth provider means invite-flow, password-reset, and session-management code needs to be built and tested from scratch in Django — budget real time for this.
- Connection pooling + `SESSION_CONTEXT` interaction (above) is a genuine footgun class specific to this architecture. Budget real test time for it.

---

## Registration & Access Architecture

**Status:** Approved.

**Decision:** Self-serve school registration is in V1, gated by manual approval before activation.

**Flow:**
1. A prospective school submits a registration form — school name (mandatory) plus basic contact/owner details.
2. The Institution record is created with `status = 'pending'`. No Owner account can log in yet.
3. A **Super Admin** (AdminPilot platform staff — a role that exists outside the Owner/Administrator/Bursar/Staff set entirely, and is not scoped to any institution) reviews pending registrations and approves or rejects.
4. On approval, `status = 'approved'` and the Owner account becomes active. On rejection, the Institution record is retained with `status = 'rejected'` (not deleted — same soft-state philosophy used elsewhere in the schema) for record-keeping.

**Access model:** Single shared login page for institution users, no subdomain. A user's account is tied directly to one institution; the backend resolves which institution from the authenticated session. This was a deliberate simplification: subdomains would add real infrastructure (wildcard DNS, wildcard TLS, hostname-parsing middleware) without doing any actual isolation work — the database-level Security Policy is what provides the isolation guarantee, not the hostname a request arrives on. Revisit only if a future customer has a genuine branding need for it; it can be added as a routing layer in front of the same backend without touching the data model.

**Super Admin login is separate (CR-004):** Super Admin accounts are not institution users and carry no `institution_id`, so they authenticate at a distinct `/platform/login/` (same Django auth machinery, different form/URL). This keeps non-tenant accounts out of the tenant-resolution code path entirely. The "single shared login page" above applies to institution users only.

**Downstream effects of this decision:**
- Bulk student import is in V1 scope — self-serve registration means any school can sign up, including ones with an existing digital roster, not just a single hand-held pilot school. See `05_Scope_Boundary.md`.
- A Super Admin role and a pending-approval Institution state are schema/permission surface that exists specifically to support this flow.

---

## Architecture Decisions Still Open

- Staging/production environment separation for a Django + MSSQL target — deferred until a deployment target exists. Local dev is settled: `settings/base.py` + `settings/dev.py`, secrets from `.env`.
- File/photo storage backend — deferred until a feature actually needs it

## ADR-003 — Application structure

Seven Django apps: `core`, `accounts`, `platform`, `academic`, `students`, `billing`, `reports` (CR-005). The module boundaries already exist in `03_Views_and_Endpoints.md` and `04_Permission_Matrix.md`; mirroring them in app structure makes them enforceable rather than conventional.

Two consequences worth stating explicitly:

- **`platform` is its own app** so that "Super Admin never touches institution data" is verifiable by inspecting one directory for tenant-model imports, rather than by trusting a permission check. It has its own login (`/platform/login/`) and no `institution_id` anywhere in it.
- **`django.contrib.admin` is not installed** (CR-004). Nothing in the specs uses it, and pointed at RLS-protected tables it would either return confusing empty results or need session-context plumbing built solely for its benefit — avoidable surface area while the isolation guarantee is still being established.

`core` owns `TenantScopedModel`, the session-context middleware, the counter tables, and AuditLog, so no app imports another's models circularly. `billing` stays whole because its atomic payment → counter → receipt → credit transaction spans several tables and splitting it would scatter one transaction across app boundaries.
