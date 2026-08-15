# AdminPilot — Claude Code Project Instructions

**Status:** Foundation set. This is a Django + MSSQL port of an original Next.js/Supabase blueprint, with deliberate scope divergences noted in `docs/05_Scope_Boundary.md`. Read that document's "Divergences" section before assuming anything about tenancy/registration matches typical white-glove SaaS patterns.

## What this is

Multi-tenant SaaS platform for school administration — student records and fee/payment management. Each school ("Institution") is a fully isolated tenant. V1 targets a handful of schools (2–5 in year one) via **self-serve registration with manual approval** — this is a deliberate divergence from the original blueprint, which planned white-glove-only onboarding. See `docs/01_Architecture.md`.

## Stack (fixed — do not substitute without discussing first)

- **Backend/ORM:** Django
- **Database:** Microsoft SQL Server, via the `mssql-django` backend
- **Isolation:** Shared database, shared schema, mandatory `institution_id` on every tenant-scoped table, enforced at the database level via SQL Server Row-Level Security (Security Policies + predicate functions) — not application code alone

## Non-negotiable rules

1. **Tenant isolation is a database-level guarantee, not a backstop.** Every tenant-scoped table needs its RLS Security Policy in place before a feature touching it is "done." Never rely on a `WHERE institution_id = ...` in application code as the *only* protection — see `docs/02_Database.md`.
2. **`institution_id` is set via `SESSION_CONTEXT`, per-request, not per-connection.** Django reuses pooled connections across requests — the middleware that sets session context must run on every request, not just at connection-open. Getting this wrong silently leaks isolation. See `docs/02_Database.md` for the exact mechanism.
3. **Super Admin (platform staff) never reads institution data.** The Super Admin role exists only to approve/reject/suspend Institution accounts. It has no code path into any institution's students, fees, or payments — that would break the data/owner independence guarantee the whole platform is built on.
4. **Fee Structures lock once a payment exists against them.** Corrections after that point go through a per-student `StudentFeeAssignment` adjustment with a required reason — never a retroactive edit to the shared structure.
5. **Every sensitive mutation writes an AuditLog entry, server-side, in the same transaction as the mutation.**
6. **Changes to anything in `/docs` go through `docs/06_CR_Process.md`** — don't silently redefine scope, schema, or roles mid-build. It's a short log-and-edit process, not a ceremony.

## Roles

Owner · Administrator · Bursar (financial modules only, field-restricted student access) · Staff (inert, reserved for future Teacher/Parent) · Super Admin (platform-level, registration approval only — not institution-scoped)

Full detail: `docs/04_Permission_Matrix.md` — the authoritative role grid, including Bursar field-level restrictions.

## Read before building

1. `docs/01_Architecture.md` — tenancy model, stack rationale, registration/access architecture
2. `docs/02_Database.md` — models, RLS enforcement, fee/payment/credit business logic
3. `docs/03_Views_and_Endpoints.md` — every URL and its role gating
4. `docs/05_Scope_Boundary.md` — what's in V1, what's deferred, and where this diverges from the original blueprint
5. `docs/07_Implementation_Roadmap.md` — build order and phase exit conditions
6. `docs/08_UI_UX.md` — screen layout/behavior; applies to every template

`06_CR_Process.md` is the change process, not a spec — read it before *changing* docs, not before building. Nothing in the docs supersedes `05_Scope_Boundary.md`'s Out of Scope list; if a build step seems to require something from it, that's a scope question, not an implementation detail.

## Project structure

Seven apps (CR-005), mirroring the module boundaries in `03_Views_and_Endpoints.md`:

| App | Contents |
|---|---|
| `core` | Institution, `TenantScopedModel` + manager, receipt/admission counters, session-context middleware, AuditLog, institution settings & setup |
| `accounts` | User (custom), roles, login/logout/password reset, self-serve registration |
| `platform` | Super Admin only — separate app so "no code path into tenant data" is verifiable by inspecting one directory. Own login at `/platform/login/` |
| `academic` | Session, Term, Class |
| `students` | Student, StudentEnrollment, bulk import (staging tables + upload/preview/commit flow) |
| `billing` | FeeStructure + items, StudentFeeAssignment, Payment, Receipt, CreditTransaction, and the service layer holding the atomic transactions |
| `reports` | Four report types, CSV export, Owner-only data export |

`django.contrib.admin` is deliberately not installed (CR-004). Settings are split: `AdminPilot/settings/base.py` + `dev.py`, selected via `DJANGO_SETTINGS_MODULE`. Secrets load from `.env` (never committed; `.env.example` is).

## Coding conventions

- No `institution_id` filtering left implicit — every tenant-scoped model inherits from a common `TenantScopedModel` base (see `docs/02_Database.md`) so the requirement is structural, not a convention someone can forget.
- Business logic that must be atomic (payment + receipt + counter increment, fee-structure locking) uses Django `transaction.atomic()` — don't split these across separate view calls.
- Never return a raw database error to an API caller. Log server-side, return a generic message.
