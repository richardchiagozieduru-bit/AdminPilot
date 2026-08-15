# AdminPilot — Database Architecture (v1.0)

**Status:** Authoritative. Narrative companion to the eventual Django `models.py` / migration files (which don't exist yet — this document defines what they must implement).

## Core Architecture

Shared database, shared schema, multi-tenant isolation via mandatory `institution_id` on every tenant-scoped table, enforced by SQL Server Row-Level Security (Security Policies + predicate functions) — not application-layer checks alone. Full mechanism in `01_Architecture.md`'s ADR-001.

## Institution Status (supports self-serve + approval)

`institutions.status`: `pending` → `approved` | `rejected`. Only `approved` institutions can have active user logins — this is the schema surface that makes the approval workflow in `01_Architecture.md` real.

`institutions.code` — editable only until the first `receipts` row exists for that institution, then permanently read-only (it's embedded in every receipt and admission number going forward: `[Code]-[Year]-[Sequence]`). Application layer enforces this via an existence check before allowing the field to change; no schema-level constraint needed, since it's a one-directional state check, not a data-integrity rule.

## Platform Users (Super Admin, not institution-scoped)

Super Admin accounts live in a separate table/model from institution-scoped `users` — **not** a role value on the same table. This is deliberate: it must be structurally impossible for a Super Admin row to carry an `institution_id` and get swept up in ordinary tenant-scoped queries or the tenant isolation Security Policy. Super Admin's only job is reviewing/approving/rejecting `institutions.status` — no code path exists from a Super Admin account into any institution's students, fees, or payments.

## Institution-Scoped Roles

`users.role` ∈ `Owner`, `Administrator`, `Bursar`, `Staff` (inert in V1). One Owner per institution, created automatically on approval. Owners invite Administrators and Bursars. Full permission detail lives in `04_Permission_Matrix.md`, which is authoritative for role access.

**Bursar field-level restriction:** Bursar has full access to Fee/Payment modules but a restricted, field-level view of Student records (name, admission number, class, fee structure, balance, payment history, guardian phone only — no DOB, address, or other personal fields). Implemented as a restricted `ModelForm` and a role-aware queryset used specifically for Bursar-role requests (see `01_Architecture.md`'s View Layer Pattern) — not a difference hidden only in the template. The tenant isolation Security Policy controls *which rows* a role sees; this is a separate mechanism layered on top for *which columns*.

## Entity Relationship Overview

```
Institution (1) ──< Users
Institution (1) ──< Sessions ──< Terms
Institution (1) ──< Classes
Institution (1) ──< Students ──< StudentEnrollments >── Classes/Terms/Sessions
Institution (1) ──< Students ──< CreditTransactions >── Payments, StudentFeeAssignments
Institution (1) ──< FeeStructures ──< FeeStructureItems
Institution (1) ──< StudentFeeAssignments >── Students, FeeStructures
Institution (1) ──< Payments >── StudentFeeAssignments, Users(recorded_by)
Institution (1) ──< Receipts >── Payments
Institution (1) ──< AuditLogs >── Users
Institution (1) ──< InstitutionNumberSequences (one row per kind, per year)

PlatformUsers (Super Admin — NOT institution-scoped, NOT subject to tenant RLS)
```

Every table above except `PlatformUsers` carries `institution_id` and the tenant isolation Security Policy from `01_Architecture.md`.

**StudentEnrollment** — join entity between Student, Class, Session, and Term. Append-only: a student's class assignment is never overwritten, giving historical continuity and, as a side effect, everything needed to support student promotion later without a schema change (deferred feature, not deferred schema design).

**FeeStructureItem** — itemized line (tuition, PTA, exam fee, transport, etc.) under a FeeStructure. `FeeStructure.total_amount` is a cached sum, recalculated on write **only while unlocked**.

**StudentFeeAssignment** — student-specific version of a FeeStructure, allowing waivers/adjustments without mutating the class-wide structure.

**Fee Structure locking:** `fee_structures.locked` is set `true` the moment the first payment is recorded against any of its student assignments. Once locked, the itemized breakdown and total are read-only — corrections happen exclusively through per-student `StudentFeeAssignment` adjustments (reason required, audit-logged), never by editing the shared structure retroactively. Implemented as logic inside the payment-recording service function, wrapped in the same `transaction.atomic()` block as the payment insert. **Application-layer only (CR-002):** every write path to fee structures already passes through the service layer, so an `AFTER UPDATE` trigger on `fee_structures` would guard a path that cannot be reached any other way — it was considered and rejected to keep hand-written SQL migration surface minimal.

**CreditTransactions** — append-only ledger. A positive row is created automatically whenever a payment exceeds the outstanding balance it's applied against (the excess, linked via `source_payment_id`); a negative row is created when a Bursar **manually** applies existing credit to a new assignment (linked via `applied_to_assignment_id`) — credit is never auto-applied. `students.credit_balance` is a cached running total kept in sync at write time, so the UI doesn't sum the ledger on every page load; the ledger remains the audit trail.

**Balance calculation:** an assignment's outstanding balance is `amount_due − (SUM(payments.amount for this assignment) − SUM(positive credit_transactions sourced from those payments))`.

**Receipt numbering** — `[InstitutionCode]-[Year]-[SequenceNumber]` (e.g. `PERM-2026-000123`), generated via `institution_number_sequences` (per-institution, per-kind, per-year counter, `kind = 'receipt'`) rather than `COUNT(*)`, to avoid race conditions under concurrent payments. The caller takes a row lock (`select_for_update`) on the counter row; payment insert + counter increment + receipt insert happen inside one `transaction.atomic()` block.

**Admission numbering** — same pattern and the same table with `kind = 'admission'`: `[InstitutionCode]-[Year]-[SequentialNumber]`, platform-generated at student creation (not school-supplied).

One table with a `kind` column rather than two separately named counter tables (CR-008). Unique on `(institution, kind, year)`, so a third counter is a row rather than a migration.

## Bulk Import Data Model (Decided: multi-worksheet-per-class format)

Each Class already defined for the institution gets its own worksheet tab in the import template — the tab name **is** the class name, validated by construction rather than a per-row text field. This means Class Setup (`01_Architecture.md`'s Institution Setup) must be complete before bulk import is usable — the template is generated dynamically from the institution's current active Class list at download time, not a static file.

**Template columns per worksheet** (no Class column needed — implied by the tab; no Admission Number column — system-generated): First Name, Middle Name, Last Name, Gender, Date of Birth, Father's Name, Mother's Name/Guardian Name, Guardian Phone, Guardian Email, Address. Session/Term/Date of Admission/Student Status are selected once for the whole import batch, not per row. Passport Photo is not part of bulk import — no file-embedding-in-Excel handling in V1; photos are added individually per student later if needed.

**Staging tables** (temporary, not `students` directly — mirrors the "nothing is written to `students` until confirmed" principle):

```
BulkImportBatch
  id, institution_id, uploaded_by (user_id), session_id, term_id, status (parsing/ready/committed), created_at

BulkImportRow
  id, batch_id (FK), sheet_name (the worksheet tab this row came from), row_number,
  first_name, middle_name, last_name, gender, date_of_birth,
  father_name, mother_name, guardian_phone, guardian_email, address,
  validation_status (valid/error), error_reason, selected_for_commit (bool, default true)
```

**Validation rules:**
- A worksheet tab name that doesn't match an existing active Class for the institution is a hard error for **every row on that tab** — not an auto-created class, and not silently skipped without being reported.
- An active Class with no corresponding worksheet in the uploaded file is not an error — it simply has no students imported this round.
- Per-row validation: required fields present (First Name, Last Name, Gender, Date of Birth, Guardian Name, Guardian Phone at minimum, matching the required/optional split used elsewhere), duplicate detection against both other rows in the same batch and existing `students` for the institution (by name + date of birth, since there's no admission number yet to key on).
- On commit: `Student` + `StudentEnrollment` (+ `StudentFeeAssignment` if an active Fee Structure exists for that class/term) created per selected valid row, admission number generated per row from the institution's counter, all inside one transaction. Staging rows are deleted after a successful commit.

See `03_Views_and_Endpoints.md` for the upload → preview → confirm view flow.

**AuditLog** — append-only, no updates/deletes at the application layer. Written server-side inside the same transaction as the mutation it records — never trust a client to report its own audit event.

## Tenant Isolation Verification Checklist (Phase 1 gate — must pass before Phase 2/Auth begins)

1. **Cross-tenant read blocked** — Institution A's authenticated session querying any tenant table returns zero rows belonging to Institution B, tested on at least: students, payments, users, receipts.
2. **Cross-tenant insert/update blocked** — Institution A's session attempting to write a row with Institution B's `institution_id` is rejected by the database (via `BLOCK PREDICATE`), not silently corrected or allowed.
3. **No session context set → zero rows** — a query executed without `SESSION_CONTEXT('institution_id')` having been set (e.g. an unauthenticated or misconfigured request) returns zero rows on every tenant table, failing closed rather than throwing an unhandled error that might leak schema details.

   **One exception, on `users` only, for reads only (CR-007).** Authentication cannot satisfy this rule: finding the account by email is what tells the request which institution to stamp, so at lookup time nothing is stamped. `users` therefore carries a filter predicate that also passes while `SESSION_CONTEXT('auth_lookup')` is 1, set only around the credential lookup and the session-user reload and cleared in a `finally`. Both BLOCK predicates on `users` keep the strict function, so the exemption can read a row across tenants but never write one; the flag appears in no other table's predicate. Test as part of this item: inside the exemption a foreign `users` row is visible but an INSERT or UPDATE against it is refused by the database, and `students` still returns zero rows.
4. **Connection reuse does not leak institution context** — specifically test: Request A (Institution 1) followed immediately by Request B (Institution 2) on a *reused pooled connection* — confirm Request B sees only Institution 2's data, not stale context from Request A. This is the highest-risk scenario called out in `01_Architecture.md` — do not skip it.
5. **Super Admin queries never touch tenant tables** — confirm the Super Admin code path has no query capable of reading `students`, `payments`, or any institution-scoped table, structurally (not just "the UI doesn't expose it").
6. **Institution status gate** — a `pending` or `rejected` institution's Owner account cannot authenticate/access any screen, even if the row and credentials otherwise exist.

This checklist must pass before any application code is built on top of it — catching a gap here is cheap; catching it after several phases are built on top is not.

## Out of Scope for This Document

- Full Django model field definitions (types, `max_length`, etc.) — belongs in the actual `models.py` once written, not this narrative document.
- View-level validation rules — see `03_Views_and_Endpoints.md`.
