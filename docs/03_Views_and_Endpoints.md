# AdminPilot — Views & Endpoints (v1.0)

**Status:** Authoritative. Every screen/URL, its Django view, and role gating. Companion to `02_Database.md` (data model) and `04_Permission_Matrix.md` (the authoritative role grid, including Bursar field-level restrictions).

**Conventions:**
- Views resolve `institution_id` from the authenticated user's session — never from a client-supplied parameter. No view accepts `institution_id` as input.
- All CBVs touching tenant-scoped models inherit `TenantScopedQuerysetMixin` (`01_Architecture.md`).
- Role checks happen via a `role_required` decorator/mixin referencing the Permission Matrix — not scattered per-view `if` checks.
- Mutating views write an AuditLog entry server-side, inside the same transaction as the mutation.
- Most URLs render a full page on `GET` and process a form on `POST`, standard Django convention. Only a small number of endpoints return `JsonResponse` — marked explicitly. Everything else is a page render, redirect, or file response.

---

## Auth & Registration

| URL | View | Purpose | Access |
|---|---|---|---|
| `/register/` | `InstitutionRegisterView` (GET/POST) | Public registration form — school name (mandatory) + owner contact details. Creates Institution (`status='pending'`) + an unactivated Owner account in one transaction | Public |
| `/register/pending/` | `RegistrationPendingView` (GET) | Confirmation page shown after submitting — "your school is under review" | Public |
| `/login/` | Django's `LoginView`, subclassed | Email/password login. Rejects with a clear message (not a generic auth failure) if the user's institution `status != 'approved'` | Public || `/logout/` | Django's `LogoutView` | End session | Authenticated |
| `/password-reset/`, `/password-reset/confirm/<uidb64>/<token>/` | Django's built-in password reset views | Standard reset flow | Public / token-gated |

Registration creates the Owner directly — no separate "invite the first user" step. Owners invite Administrators/Bursars afterward, via Users & Institution Settings below.

## Super Admin (platform-level, not institution-scoped)

| URL | View | Purpose | Access |
|---|---|---|---|
| `/platform/login/` | `PlatformLoginView` (subclassed `LoginView`) | Super Admin login — separate from `/login/` because Super Admin accounts carry no `institution_id` and must not pass through tenant-resolution code (CR-004) | Public |
| `/platform/institutions/` | `PendingInstitutionsListView` | List institutions by status (pending/approved/rejected) | Super Admin |
| `/platform/institutions/<id>/approve/` | `ApproveInstitutionView` (POST) | Sets `status='approved'`, activates the Owner account | Super Admin |
| `/platform/institutions/<id>/reject/` | `RejectInstitutionView` (POST, reason required) | Sets `status='rejected'` | Super Admin |

Deliberately its own URL namespace (`/platform/...`), structurally separate from every institution-scoped view below — reinforces that Super Admin code has no shared code path into tenant data (`02_Database.md`).

## Institution Setup & Academic Structure

| URL | View | Purpose | Access |
|---|---|---|---|
| `/setup/` | `InstitutionSetupWizardView` | First-run wizard: institution details, timezone, first Session/Term, initial Classes | Owner |
| `/settings/institution/` | `InstitutionSettingsView` (GET/POST) | Edit institution details. `code` field rejected with an inline form error if any `receipts` row exists | Owner |
| `/settings/academic/` | `AcademicStructureView` (GET/POST) | Ongoing Session/Term creation | Owner |

## Dashboard

| URL | View | Purpose | Access |
|---|---|---|---|
| `/` (post-login) | `DashboardView` | Stat cards, Action Required, Recent Transactions, Quick Actions — all rendered server-side in one context | All (Bursar gets financial widgets only) |

## Global Search

| URL | View | Purpose | Access |
|---|---|---|---|
| `/search/` | `SearchView` | Full-page search results (students, payments, receipts) | All (Bursar gets field-restricted student results) |
| `/search/suggest/?q=` | `SearchSuggestView` — **returns `JsonResponse`** | Search-as-you-type dropdown | All |

## Students

| URL | View | Purpose | Access |
|---|---|---|---|
| `/students/` | `StudentListView` | List with filters (`?class_id=&status=&q=`) | Owner, Administrator (full); Bursar (restricted fields) |
| `/students/add/` | `StudentCreateView` (GET/POST) | Create. Required/optional field split. Auto-creates `StudentFeeAssignment` if an active Fee Structure exists for the class/term | Owner, Administrator |
| `/students/<id>/` | `StudentDetailView` | Profile: Overview, Payment History, Enrollment History, Activity Timeline tabs | Owner, Administrator (full); Bursar (restricted) |
| `/students/<id>/edit/` | `StudentUpdateView` (GET/POST) | Edit. Class change re-checks Fee Structure assignment (bidirectional rule) | Owner, Administrator |
| `/students/<id>/archive/`, `/students/<id>/reactivate/` | POST-only views | Soft-delete / restore | Owner, Administrator |

**Bulk Import** — a multi-step form flow, not a JSON API (see Revision Note at the end of this document). Multi-worksheet-per-class format: one worksheet per Class, tab name = class name.

| URL | View | Purpose | Access |
|---|---|---|---|
| `/students/import/template/` | `BulkImportTemplateView` (GET), returns `.xlsx` `HttpResponse` | Generates a downloadable workbook with one worksheet per active Class (tab named exactly like the class), fixed header row. Generated fresh from the current Class list each time — not a static file. Requires Class Setup to be complete; view redirects to Class setup with a message if the institution has no active classes yet | Owner, Administrator |
| `/students/import/` | `BulkImportUploadView` (GET/POST) | Upload the filled-in workbook. Validates each worksheet's tab name against active Class names — a tab that doesn't match an active class is a hard error for every row on it. Valid/error rows are staged to `BulkImportBatch`/`BulkImportRow` (see `02_Database.md`), then redirects to preview. Nothing is written to `students` yet | Owner, Administrator |
| `/students/import/<batch_id>/preview/` | `BulkImportPreviewView` (GET/POST) | Renders staged rows grouped by class/tab, tagged valid/error with reasons. Administrator can deselect specific valid rows before confirming. `POST` commits: creates `Student` + `StudentEnrollment` (+ `StudentFeeAssignment` if applicable) for each confirmed row, admission numbers generated per row, all in one transaction. Writes a single summarizing `AuditLog` entry, not one per student. Staging rows are deleted after commit | Owner, Administrator |

An active Class with no matching worksheet in the uploaded file is not an error — that class simply gets no students imported this round.

## Classes

| URL | View | Purpose | Access |
|---|---|---|---|
| `/classes/` | `ClassListView` | List, with current student count per class | Owner, Administrator (edit); Bursar (view only) |
| `/classes/add/` | `ClassCreateView` | Create | Owner, Administrator |
| `/classes/<id>/edit/` | `ClassUpdateView` | Edit (name, order, status) | Owner, Administrator |
| `/classes/<id>/deactivate/` | POST-only view | Deactivate — allowed with enrolled students, blocks new enrollments only | Owner, Administrator |
| `/classes/<id>/reactivate/` | POST-only view | Reactivate a deactivated class — the paired reverse of the button above | Owner, Administrator |

`StudentCreateView`/`StudentUpdateView` validate `class.status == 'active'` before creating a `StudentEnrollment` — enrolling into a deactivated class returns a form error, not a silent failure.

## Fee Structures

| URL | View | Purpose | Access |
|---|---|---|---|
| `/fee-structures/?term_id=` | `FeeStructureListView` | List | Owner, Administrator, Bursar |
| `/fee-structures/add/` | `FeeStructureCreateView` (with a formset for itemized lines) | Create — auto-generates `StudentFeeAssignment` for every enrolled student in that class/term | Owner, Administrator, Bursar |
| `/fee-structures/<id>/` | `FeeStructureDetailView` | Detail with items | Owner, Administrator, Bursar |
| `/fee-structures/<id>/edit/` | `FeeStructureUpdateView` | Edit items. Rejected with a form error if `locked=True` — points to the per-student adjustment view instead | Owner, Administrator, Bursar |
| `/fee-structures/<id>/assignments/` | `FeeAssignmentListView` | Student Fee Adjustments list | Owner, Administrator, Bursar |
| `/fee-assignments/<id>/adjust/` | `FeeAssignmentAdjustView` (GET/POST, reason required) | Adjust an individual student's `amount_due`, audit-logged. Only path to correct a fee after locking | Owner, Administrator, Bursar |

## Payments

| URL | View | Purpose | Access |
|---|---|---|---|
| `/payments/?date_from=&date_to=&method=&status=` | `PaymentListView` | Payment list | Owner, Administrator, Bursar |
| `/payments/add/` | `PaymentCreateView` (GET/POST) | Record payment — creates Receipt in the same transaction. Overpayment excess auto-recorded as credit. Optional "apply existing credit" checkbox applies credit toward the current balance first | Owner, Administrator, Bursar |
| `/students/<id>/credit/` | `StudentCreditView` | Credit balance + transaction history (also shown on Student Profile) | Owner, Administrator, Bursar |
| `/payments/<id>/` | `PaymentDetailView` | Payment detail | Owner, Administrator, Bursar |
| `/payments/<id>/reverse/` | `PaymentReverseView` (GET/POST, reason required) | Reverse a payment | Owner, Administrator, Bursar |

`PaymentCreateView`'s `POST` handler wraps payment insert + receipt-counter increment + receipt insert + any resulting `credit_transactions` row in one `transaction.atomic()` block — no payment is ever recorded without a receipt number, no receipt number is ever skipped or duplicated under concurrent requests.

## Receipts

| URL | View | Purpose | Access |
|---|---|---|---|
| `/receipts/<id>/` | `ReceiptDetailView` | Receipt view, styled for browser print (`@media print` CSS) — no PDF generation | Owner, Administrator, Bursar |

No standalone receipt-creation URL — receipts only ever exist as a side effect of `PaymentCreateView`.

## Reports

| URL | View | Purpose | Access |
|---|---|---|---|
| `/reports/income/?period=daily\|weekly\|monthly\|term&date_from=&date_to=` | `IncomeReportView` | Income reports. Date boundaries computed using `institutions.timezone`, not server time | Owner, Administrator, Bursar |
| `/reports/outstanding-fees/?class_id=&term_id=` | `OutstandingFeesReportView` | Outstanding Fees Report | Owner, Administrator, Bursar |
| `/reports/student/<id>/payment-history/` | `StudentPaymentHistoryReportView` | Also powers the Profile tab | Owner, Administrator, Bursar |
| `/reports/class-summary/?class_id=&term_id=` | `ClassSummaryReportView` | Class Payment Summary | Owner, Administrator, Bursar |
| `/reports/<type>/export/?format=csv` | Report-specific export view, returns `HttpResponse` (`text/csv`) | Export any report type | Owner, Administrator, Bursar |

## Users & Institution Settings

| URL | View | Purpose | Access |
|---|---|---|---|
| `/settings/users/` | `UserListView` | List institution's staff accounts | Owner |
| `/settings/users/invite/` | `UserInviteView` (GET/POST) | Add Administrator/Bursar — generates a one-time expiring setup link the Owner copies and shares (CR-003) | Owner |
| `/settings/users/<id>/edit/` | `UserUpdateView` | Edit role/status (enable/disable) | Owner |
| `/settings/audit-log/` | `AuditLogView` | Owner sees all institution activity; Administrator/Bursar see only their own actions | Owner (all), Administrator/Bursar (own) |

## Data Export

| URL | View | Purpose | Access |
|---|---|---|---|
| `/export/students/` | `StudentExportView`, returns CSV `HttpResponse` | Full student record export | Owner |
| `/export/payments/?date_from=&date_to=` | `PaymentExportView`, returns CSV `HttpResponse` | Full payment history export | Owner |

Every export view writes an `AuditLog` entry (`action='data.exported'`) before streaming the response.

---

## Cross-Cutting Notes

- **Tenant isolation is the enforcement backbone.** Every `TenantScopedQuerysetMixin`-based view is filtered by the database Security Policy regardless of what the view code does — application code cannot accidentally bypass isolation by forgetting a filter.
- **Bursar field-level restriction** is a restricted `ModelForm` and a role-aware queryset specifically for Bursar-role requests to student-touching views — decided in the view based on `request.user.role`, not left to the template to hide fields cosmetically.
- **AuditLog writes are server-side only**, inside the same transaction as the mutation.
- **Activity Timeline** reads AuditLog plus payment/enrollment tables and formats entries into plain-language strings in the view, not in JavaScript — the template renders pre-formatted lines.
- **The only genuine `JsonResponse` endpoint in this document is `SearchSuggestView`.** Everything else is a normal page render, form POST, or file response. Resist adding more JSON endpoints out of habit — see `01_Architecture.md`'s View Layer Pattern.

## Revision Note

`01_Architecture.md`'s View Layer Pattern originally listed the bulk-import flow as a candidate `JsonResponse` endpoint. Writing this document in full made the simpler option clear: a plain multi-step form flow (upload → server-rendered preview page → confirm) avoids inventing a JSON contract that nothing else needs. Revised here; `01_Architecture.md` gets a one-line correction to match rather than being left inconsistent with this document.

---

## Settled (previously open)

- **Bulk import file format:** multi-worksheet Excel, one tab per class, tab name matching an active Class exactly. Decided — see `02_Database.md`'s Bulk Import Data Model and the `/students/import/` rows above.
- **`UserInviteView` delivery:** generates a one-time, expiring setup link the Owner copies and shares manually. No email backend in V1 (CR-003). Note Django's password-reset flow above still assumes email — in dev, reset links appear in the console; this needs revisiting before real pilot schools use the system.
