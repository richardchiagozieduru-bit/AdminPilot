# AdminPilot — Change Request Process (v1.0)

**Status:** Authoritative. Referenced by `CLAUDE.md` rule 6 and `04_Permission_Matrix.md`.

Deliberately lightweight. At the scale this project operates (small team, 2–5 pilot schools), heavyweight change control would be theatre. The point of this document is narrow: **make scope, schema, and role changes visible and dated, so nobody discovers six phases later that a boundary moved silently.**

## What needs a CR

Only these four categories:

1. **Scope** — anything moving between the In Scope and Out of Scope lists in `05_Scope_Boundary.md`, in either direction.
2. **Schema** — new tenant-scoped tables, changes to tenant isolation mechanics, or changes to the entity relationships in `02_Database.md`'s ERD.
3. **Roles/permissions** — any change to the grid or the Bursar field-level restrictions in `04_Permission_Matrix.md`.
4. **Stack** — substituting anything in `01_Architecture.md`'s ADR-002 table.

## What does NOT need a CR

Normal building. Adding a view that's already specified, writing the models a doc already describes, fixing a bug, styling a screen, adding a test, correcting a stale cross-reference between docs. If the docs already say to do it, doing it isn't a change.

Resolving something already listed as **Still Open** in a doc is also not a CR — that's the decision the doc was waiting for. Log it (below), edit the doc to state the decision, and remove it from Still Open.

## The process

1. Append an entry to the log at the bottom of this file.
2. Edit the affected doc(s) so they state the new decision as fact — not as a footnote pointing here. The docs stay readable as the current truth; this log is the history.
3. If it changes something already built, say so in the entry.

An entry is:

```
### CR-NNN — <short title>
**Date:** YYYY-MM-DD · **Type:** Scope | Schema | Roles | Stack | Decision (resolving a Still Open)
**Change:** what changed, in one or two sentences.
**Why:** the reasoning, including what was rejected.
**Affects:** which docs were edited; whether any built code needs revisiting.
```

## Log

### CR-001 — Docs and code unified into one repository
**Date:** 2026-08-11 · **Type:** Decision
**Change:** `CLAUDE.md` and `docs/` moved from a sibling `adminpilot-django/` directory into the Django project tree at `AdminPilot/`, which is now a git repository. The sibling directory was removed.
**Why:** With docs in a separate tree, `CLAUDE.md` never loaded automatically when working in the code directory — the project instructions that govern every non-negotiable rule were invisible by default. One tree, one repo, instructions always in context.
**Affects:** No doc content changed. No code affected.

### CR-002 — Fee-structure locking is application-layer only; MSSQL trigger rejected
**Date:** 2026-08-11 · **Type:** Decision (resolves a Still Open in `02_Database.md` and `07_Implementation_Roadmap.md`)
**Change:** Fee-structure locking is enforced solely in the payment-recording service function inside `transaction.atomic()`, plus a `locked` check in `FeeStructureUpdateView`. The optional `AFTER UPDATE` trigger on `fee_structures` is not built.
**Why:** Every write path to fee structures already passes through the service layer, so the trigger guards a path that cannot be reached another way. It would add hand-written SQL migration complexity — the same category of migration already carrying real risk for tenant isolation — for no additional guarantee. Tenant-level database enforcement (the RLS BLOCK predicates) is unaffected and remains the floor for isolation; this decision is only about fee locking.
**Affects:** `02_Database.md` (locking section), `07_Implementation_Roadmap.md` (Phase 5, Still Open list). Nothing built yet.

### CR-003 — User invites use a one-time setup link, not email
**Date:** 2026-08-11 · **Type:** Decision (resolves a Still Open in `03_Views_and_Endpoints.md` and `07_Implementation_Roadmap.md`)
**Change:** `UserInviteView` generates a one-time, expiring setup link that the Owner copies and shares directly. No email backend is configured for V1.
**Why:** At 2–5 pilot schools an Owner adding an Administrator is in the same building or a message away. Configuring and testing a real email backend (deliverability, templates, bounce handling) is real work that buys nothing at this scale. Email delivery becomes a fast-follow CR when institution count makes manual sharing awkward.
**Affects:** `03_Views_and_Endpoints.md` (Still Open list), `07_Implementation_Roadmap.md` (Phase 8, Still Open list). Note: Django's password-reset flow in `03_Views_and_Endpoints.md` also assumes email — with the console backend in dev, reset links appear in the server log. This must be revisited before real pilot schools use the system (`07_Implementation_Roadmap.md` Phase 9).

### CR-004 — Super Admin gets its own app, its own login, and Django admin is removed
**Date:** 2026-08-11 · **Type:** Schema/Architecture
**Change:** Three related decisions about platform-level access:
- Super Admin lives in a dedicated `platform` Django app, not a module inside a shared app.
- Super Admin authenticates at `/platform/login/`, separate from the institution login at `/login/`.
- `django.contrib.admin` is removed from `INSTALLED_APPS`.
**Why:** `CLAUDE.md`'s non-negotiable rule 3 ("Super Admin never reads institution data") is only trustworthy if it is *checkable* — a dedicated app means the guarantee can be verified by inspecting one directory for tenant imports, which is exactly what `02_Database.md`'s verification checklist item 5 asks for structurally. The separate login exists because the docs specify a single shared login page for *institution users*, but Super Admin is not an institution user and has no `institution_id`; routing both through one form would put a non-tenant account through tenant-resolution code paths. Django's admin is removed because nothing in the docs uses it, and against RLS-protected tables it would either return confusing empty results or require session-context plumbing built solely for it — needless surface area while the isolation guarantee is still being established.
**Affects:** `01_Architecture.md` (registration/access architecture), `03_Views_and_Endpoints.md` (Super Admin section, auth table). Nothing built yet.

### CR-005 — Project restructured into seven Django apps
**Date:** 2026-08-11 · **Type:** Architecture
**Change:** The single placeholder `Adpilot` app is replaced by: `core` (Institution, `TenantScopedModel`, counters, session-context middleware, AuditLog), `accounts` (User, roles, auth, registration), `platform` (Super Admin), `academic` (Session, Term, Class), `students` (Student, enrollment, bulk import), `billing` (fee structures, assignments, payments, receipts, credit), `reports` (reports + exports).
**Why:** The module boundaries already exist in `03_Views_and_Endpoints.md` and `04_Permission_Matrix.md`; mirroring them in app structure makes them enforceable rather than conventional. `core` holds the shared base model and middleware so no app imports another's models circularly. `billing` stays whole because its business logic (locking, atomic payment + receipt + credit) spans several tables and splitting it would scatter one transaction across apps.
**Affects:** No doc content changed — this implements structure the docs imply. Nothing built yet.

### CR-006 — Design tokens pass added before Phase 3
**Date:** 2026-08-11 · **Type:** Decision (resolves the Still Open in `08_UI_UX.md`)
**Change:** A single `tokens.css` defining CSS custom properties for color, spacing, radius, and typography scale is written at the start of Phase 3, before the Setup Wizard and Class Management screens are built. This is a token file, not a component library or design system.
**Why:** `08_UI_UX.md` specifies layout and behavior but no visual language. Phases 3–5 build the majority of the app's screens; styling them ad hoc means a retrofit later across every template. A token file is a few hours' work and makes the Phase 9 responsiveness/polish pass a matter of adjusting values rather than rewriting stylesheets.
**Affects:** `08_UI_UX.md` (Still Open list), `07_Implementation_Roadmap.md` (Phase 3).

### CR-007 — `users` reads are exempt from the tenant filter during authentication
**Date:** 2026-08-11 · **Type:** Schema (tenant isolation mechanics)
**Change:** `users` carries a second, lenient FILTER predicate — `fn_TenantAccessPredicateAuthLookup`, which passes when the row's `institution_id` matches `SESSION_CONTEXT('institution_id')` **or** when `SESSION_CONTEXT('auth_lookup')` is 1. Both BLOCK predicates on `users` keep the strict function. The flag is set only by `auth_lookup_context()` in `core/middleware.py`, which wraps the credential lookup and the session-user reload and clears it in a `finally`. Every other tenant-scoped table is unchanged: strict FILTER, strict BLOCK on INSERT and UPDATE.

**Why:** Authentication has a bootstrap problem that the strict predicate cannot express. Finding the account by email is *what tells the request which institution to stamp*, so at lookup time nothing is stamped and the row is invisible — no login can ever succeed. The same applies on every subsequent request, where `AuthenticationMiddleware` reloads `request.user` from the session before any institution is known.

Four alternatives were rejected:
- **Take `users` out of RLS entirely.** Removes cross-tenant protection permanently rather than for the width of one lookup, and `users` holds password hashes.
- **A separate non-tenant table mapping email → institution.** Two rows to keep in sync per account, and the mapping table becomes a new place to enumerate every school's staff.
- **A privileged second connection with RLS bypassed.** Needs a login with `UNMASK`/policy-bypass rights and its credentials in `.env`; that connection could read every table, not one.
- **Resolve the institution from the URL (per-school subdomain).** A scope change — `05_Scope_Boundary.md` specifies one shared login — and it discloses which schools exist.

What keeps the exemption narrow, each now pinned by a test in `core/tests/test_tenant_isolation.py`:
- **Read-only.** Inside the exemption another institution's user row is visible, so an UPDATE can match it — and the strict BLOCK AFTER UPDATE raises. Same for INSERT. (`test_the_auth_exemption_is_read_only`)
- **One table.** The flag appears in no other predicate, so a `auth_lookup_context()` left open longer than intended still cannot read `students` or anything else across tenants. (`test_the_auth_exemption_does_not_extend_past_users`)
- **Cleared twice.** By the context manager's `finally`, and again by `TenantContextMiddleware` before the connection returns to the pool — a connection must never be pooled with the exemption open.
- **At most one row.** Email is globally unique, so the lookup this exists for can only ever see the one account it was given credentials for.

**Affects:** `02_Database.md` — verification checklist item 3 amended to state the exemption and its bounds, since item 3 as written ("zero rows on every tenant table") is no longer literally true of `users`. Item 1 is unchanged and still holds: outside the exemption, A's session reads none of B's user rows. Already built and depended on by Phase 2: `core/migrations/0002_tenant_rls.py`, `core/middleware.py`, `accounts/backends.py`, `accounts/views.py`. This CR is late — the mechanism was built during Phase 2 and should have been logged then.

### CR-008 — One `institution_number_sequences` table replaces the two counter tables
**Date:** 2026-08-11 · **Type:** Schema
**Change:** `02_Database.md`'s `institution_receipt_counters` and `institution_admission_counters` become a single tenant-scoped table, `institution_number_sequences`, with a `kind` column (`admission` | `receipt`), unique on `(institution, kind, year)`. The numbering formats, the `select_for_update()` row lock, and the requirement that the increment share a transaction with the record it numbers are all unchanged.
**Why:** The two tables were specified with identical columns, identical per-institution-per-year keying, and identical locking discipline — the only difference was which number they hand out. One table means one implementation of the lock-and-increment path instead of two that can drift, and a future counter (invoice or credit-note numbers) becomes a row rather than a migration. Rejected: keeping both tables as documented, which buys nothing for the duplicated logic; and deriving numbers from `MAX()`/`COUNT(*)`, which `02_Database.md` already rules out for reusing numbers after a delete and racing under concurrency.
**Affects:** `02_Database.md` (ERD line, receipt numbering and admission numbering paragraphs), `07_Implementation_Roadmap.md` (Phase 1 model list). Built: `core/models.py` (`InstitutionNumberSequence`) and the Phase 1 migrations, including the table's RLS policy. No consumer exists yet — admission numbers are generated in Phase 4 and receipt numbers in Phase 5 — so nothing needs revisiting.

### CR-009 — `/classes/<id>/reactivate/` added to the endpoint table
**Date:** 2026-08-11 · **Type:** Decision
**Change:** A POST-only `ClassReactivateView` at `/classes/<id>/reactivate/`, Owner and Administrator, is added to `03_Views_and_Endpoints.md`'s Class Management table.
**Why:** Not new capability. `ClassUpdateView` already lists `status` among its editable fields, so a deactivated class could always be brought back through the edit form; the doc simply had a deactivate endpoint with no counterpart. Making the reverse a paired button is what `08_UI_UX.md` implies for a reversible state, and it keeps the audit trail explicit — `class.reactivated` rather than a generic `class.updated` that happens to have flipped a status. Logged rather than edited in silently because the endpoint table is what `04_Permission_Matrix.md` cross-references for access.
**Affects:** `03_Views_and_Endpoints.md` (Class Management table). Code already built in `academic/views.py`; covered by `test_reactivation_restores_the_class`.
