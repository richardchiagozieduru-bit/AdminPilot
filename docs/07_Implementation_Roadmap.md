# AdminPilot — Implementation Roadmap (v1.0)

**Status:** Authoritative. Build sequencing, ordered by dependency — each phase's output unblocks the next. Not a calendar. Each phase has a binary exit condition before moving to the next, same discipline as `Phase0_Acceptance_Checklist.md`-style gating (a dedicated checklist doc for this port doesn't exist yet — treat each phase's exit condition below as the gate until one does).

---

### Phase 0 — Environment Setup
- Initialize the Django project; connect to MSSQL via `mssql-django`
- Settle the still-open deployment/environment question (`01_Architecture.md`) — at minimum, separate settings for local dev vs. a shared dev database, before any real data touches it
- Confirm no secret (DB credentials, `SECRET_KEY`) is committed to the repo — `.env` pattern, `.env.example` committed instead
- **Exit condition:** an empty Django app runs locally and successfully connects to an MSSQL dev database.

### Phase 1 — Database Foundation
- Write `models.py` for every entity in `02_Database.md`'s ERD, including `BulkImportBatch`/`BulkImportRow`, `institution_number_sequences`
- Every tenant-scoped model inherits `TenantScopedModel` (`01_Architecture.md`)
- Generate and apply migrations for the model-derived schema
- Write the raw-SQL migration (`migrations.RunSQL`) for the Security Policy + predicate function per tenant-scoped table — this is **not** derived from `models.py`, has to be hand-written and applied separately (flagged in an earlier exchange — don't let this get missed)
- Build the `SESSION_CONTEXT`-setting middleware and confirm it runs on every request, not just connection-open
- Run the full Tenant Isolation Verification Checklist from `02_Database.md` — all six items
- **Exit condition:** the verification checklist passes in full. This is the highest-leverage checkpoint in the whole roadmap — every later phase assumes tenant isolation holds. Do not proceed past this on a partial pass.

### Phase 2 — Auth & Registration
- Django's built-in auth, extended with `institution_id`/`role` fields on the user model
- Build `InstitutionRegisterView`, `RegistrationPendingView` — self-serve registration creating a `pending` Institution + unactivated Owner
- Build the Super Admin app/namespace: `PendingInstitutionsListView`, `ApproveInstitutionView`, `RejectInstitutionView` — confirm this code path has zero queries touching any tenant-scoped table (per the Phase 1 checklist's item 5, re-verified here against real views)
- Subclass `LoginView` to reject non-`approved` institutions with a clear message
- Wire up Django's password reset flow
- **Exit condition:** a test registration can be submitted, approved by a Super Admin account, and the resulting Owner can log in and reach an empty Dashboard. A `pending` or `rejected` institution's Owner cannot log in.

### Phase 3 — Institution Setup & Core Reference Data
- Design tokens pass (CR-006): write `static/css/tokens.css` (color, spacing, radius, typography scale) so screens aren't styled ad hoc
- First-run Setup Wizard (institution details incl. timezone → Session/Term → Classes)
- Institution Settings (name/type/timezone editable; `code` editable only until first receipt exists)
- Academic Structure screen (ongoing Session/Term creation)
- Class Management (full CRUD; deactivation allowed with active students, blocks new enrollments only)
- **Exit condition:** a freshly-approved Owner can complete setup and reach a populated (empty-but-configured) Dashboard with at least one Class defined.

### Phase 4 — Student Management
- Student List, Add/Edit (required/optional split), Profile (Overview, Payment History tab stub, Enrollment History tab, Activity Timeline tab stub — full data wiring depends on Phase 5/6)
- Archive/reactivate
- **Bulk Import**, full flow: `BulkImportTemplateView` (dynamic multi-worksheet generation from the current Class list) → `BulkImportUploadView` (parse, validate per-sheet against active Classes, stage rows) → `BulkImportPreviewView` (review, deselect, commit)
- **Exit condition:** students can be added individually and via the full template-download → upload → preview → commit bulk import flow, and found via the Student List filter/search.

### Phase 5 — Fee & Payment Engine
- Fee Structure create/edit (itemized), auto-assignment to enrolled students, individual adjustments
- **Locking:** verify the fee-locking logic (application-layer service + `FeeStructureUpdateView` form error — no MSSQL trigger, CR-002) fires correctly and the edit view is rejected once locked
- Payment recording, including the atomic payment + receipt-counter + receipt transaction
- **Overpayment/credit:** verify the credit-balance logic, the "apply existing credit" option on Record Payment, and credit visibility on Student Profile
- Payment reversal (reason required)
- Receipt view (browser print, `@media print` CSS)
- **Exit condition:** a full record-student → assign-fee → record-payment (including at least one deliberate overpayment) → view-receipt loop works end to end for one student, with credit correctly reflected afterward.

### Phase 6 — Dashboard, Search, Activity Timeline
- Dashboard stat cards, Action Required, Recent Transactions, Quick Actions — server-rendered in one view
- `SearchView` (full page) + `SearchSuggestView` (the one `JsonResponse` endpoint in the app — search-as-you-type)
- Activity Timeline (AuditLog → plain-language rendering in the view), wired into Student Profile
- **Exit condition:** Dashboard accurately reflects data entered in Phases 4–5; search finds a student/payment/receipt from any screen, both via full search and the live-suggest dropdown.

### Phase 7 — Reports & Export
- Reports Hub + all four report types (Income, Outstanding Fees, Student Payment History, Class Summary) + CSV export
- Timezone-aware date boundaries using `institutions.timezone`, not server time — a specific test case around a payment made near local midnight
- Data Export (Students, Payments — Owner only)
- **Exit condition:** every report type produces correct numbers against Phase 4–5 test data, including a boundary test that would fail if server/UTC time were used instead of institution timezone, and CSV exports open cleanly in Excel/Sheets.

### Phase 8 — Users, Roles & Audit
- Settings > Users (invite Administrator/Bursar — resolve the still-open "real email vs. manual link" question here, since it can't stay open past this phase; enable/disable)
- `role_required` decorator/mixin finalized against the full `04_Permission_Matrix.md` grid
- Audit Log tiered visibility
- Bursar field-level restriction verified against the restricted `ModelForm`/queryset
- **Exit condition:** log in as each of Owner/Administrator/Bursar and confirm the Permission Matrix holds exactly, including what each role cannot see or do. Confirm Staff has zero access, matching its inert-in-V1 status.

### Phase 9 — Polish & Pilot Readiness
- Empty states, loading states, mobile responsiveness pass
- Load realistic pilot data across at least two institutions simultaneously, specifically to re-exercise the Phase 1 tenant isolation checklist under real multi-tenant conditions, not just synthetic test data
- End-to-end walkthrough of every module as a single QA pass
- **Exit condition:** ready to open registration to real pilot schools.

---

## Notes on Sequencing

- **Phase 1's tenant isolation checklist is the highest-leverage checkpoint in this roadmap.** Every later phase assumes it holds. A gap caught in Phase 8 instead of Phase 1 means re-auditing everything built in between.
- **Phase 9 is the first point two real institutions coexist with real data** — worth deliberately re-running the cross-tenant read/write checks from Phase 1 here, not just trusting they still hold because they passed once against synthetic data.
- Phases 4 and 5 are the largest by volume of screens/logic — natural parallelization split if the team wants it (Student Management vs. Fee/Payment engine), since they share the schema but touch different views until the Payment Recording flow's student-lookup step.
- Nothing in `05_Scope_Boundary.md`'s "Out of Scope" list should appear as a task in any phase above — if it does during actual build, that's a scope-boundary violation worth flagging before building it, not after.

## Still Open (affects this roadmap directly)

- Deployment/environment separation — a `settings/base.py` + `settings/dev.py` split with `.env`-loaded secrets now exists, which satisfies Phase 0's "separate settings for local dev vs. a shared dev database." Staging/production separation remains undecided and is deferred until a deployment target exists — it does not block any phase before 9.

## Settled (previously open)

- **User invite: one-time setup link, not email** (CR-003). Phase 8 builds `UserInviteView` generating an expiring link; no email backend. Password-reset email usage stays console-only through Phase 9 and must be revisited before real pilot schools sign up.
- **Fee-locking: application-layer only** (CR-002). The MSSQL-trigger option is rejected; Phase 5's locking step verifies the service-layer `locked` logic plus the `FeeStructureUpdateView` form error, and nothing else.
- **Design tokens** (CR-006): added as the first item of Phase 3.
- **`users` reads are exempt from the tenant filter during authentication** (CR-007). Phase 1's isolation checklist item 3 now covers the exemption and its bounds explicitly — read-only, `users` only — so Phase 2's login path does not read as a gap in the gate it had to pass.
- **One counter table, not two** (CR-008). `institution_number_sequences` with a `kind` column; Phase 4's admission numbers and Phase 5's receipt numbers both take a `select_for_update()` lock on a row of it.
