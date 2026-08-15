# AdminPilot — UI/UX Design (v1.0)

**Status:** Authoritative. Screen-by-screen layout and flow, for server-rendered Django templates + vanilla JS (`01_Architecture.md`'s View Layer Pattern). Companion to `03_Views_and_Endpoints.md` — every screen below maps to a URL/view defined there. Visual language (card-based dashboard, wireframe layout) is inherited from the original AdminPilot concept mockups; screen *behavior* here is new, adapted for self-serve registration and the multi-worksheet import flow neither of which existed in that original concept.

## Template Architecture

- `base.html` — shared shell: top nav (institution name, logged-in user, logout), left sidebar (module links, filtered by `request.user.role` against `04_Permission_Matrix.md`), a `{% block content %}` for page body.
- Role-based sidebar filtering happens server-side in the base template's context (a `visible_modules` context processor reading the Permission Matrix), not hidden via CSS — a Bursar's rendered page never contains markup for User Management or Institution Settings at all.
- `base_print.html` — a separate, minimal shell (no nav/sidebar) used only by the Receipt view, styled for `@media print`.
- `base_public.html` — a separate shell for Registration and Login (no authenticated nav at all).

---

## Public Screens

### Registration (`/register/`)
Single form: School Name (required), Owner Name, Owner Email, Owner Phone, Password. On submit, shows a clear message: *"Thanks — your school is under review. We'll email you once approved."* No dashboard preview, no partial access — nothing is clickable until Super Admin approval flips the institution to `approved`.

### Login (`/login/`)
Email + password. If the account's institution is `pending`, the error message says so explicitly ("Your school's registration is still under review") rather than a generic "invalid credentials" — this is a deliberate exception to normal auth-error vagueness, since the person isn't guessing a password wrong, they're waiting on a real, known state.

---

## Super Admin Screens (`/platform/...`)

### Pending Institutions List
A simple table: School Name, Owner Email, Submitted Date, Status filter (pending/approved/rejected). Each pending row has Approve / Reject buttons inline — Reject opens a small reason field (required) before confirming. No student, fee, or payment data is reachable from anywhere in this section — structurally, not just by omission from the nav.

---

## Main Dashboard (`/`)

Card-based layout, matching the original concept mockup's structure directly:

- Greeting header ("Good morning, [Owner name]") + today's date
- Stat cards row: Students, Classes, Fees Due, Collected (this term)
- Second row: Received Today, Action Required (owing >30 days count, incomplete records count — computed server-side in `DashboardView`'s context, not via JS)
- Quick Actions: Add Student, Record Payment, Search, Reports — each a plain link/button to its respective view
- Recent Payments feed: last several payments, server-rendered, with a "View all" link to the full Payment List

**Bursar's dashboard** renders the same template with a reduced context — `DashboardView` passes only financial widgets when `request.user.role == 'Bursar'`, so the template's student-count/class-count cards simply aren't in the context to render, not hidden via `{% if %}` on data that shouldn't have been sent to the template at all.

---

## Class Page (`/classes/<id>/`)

Matches the original concept's "zoomed in" framing: class teacher, student count, a Fees Paid / Fees Owing / Fully Paid summary row, then the student list itself with inline fee status. "Add Student to Class" links to `StudentCreateView` pre-filled with this class. No attendance or grades data — out of scope per `05_Scope_Boundary.md`.

---

## Student Profile (`/students/<id>/`)

Tabbed layout: Overview, Payment History, Enrollment History, Activity Timeline. Tabs are server-rendered separate views (not client-side tab-switching over pre-loaded data) — each tab click is a normal page load to its own URL, keeping this consistent with "plain Django views, minimal JS."

Bio Info / Guardian Info two-column layout for Owner/Administrator. **Bursar's version of this page renders a visibly different Overview tab** — the restricted field set from `04_Permission_Matrix.md`, not the same fields grayed out. A Bursar should never see a DOB field that's merely disabled; the field shouldn't be in the rendered HTML at all.

---

## Bulk Import Flow (`/students/import/...`)

Three-screen sequence, each a full page load — deliberately not a single-page wizard with JS-managed steps:

1. **Import Landing** (`/students/import/`, `GET`) — explains the flow, a "Download Template" button (`BulkImportTemplateView`) generating the multi-worksheet `.xlsx` from the institution's current active Classes, and a file upload form below it for the completed workbook.
2. **Preview** (`/students/import/<batch_id>/preview/`) — a table grouped by class/worksheet tab, each row tagged Valid (green) or Error (red, with the specific reason inline — e.g. "Missing Date of Birth", "Tab 'JSS 1C' doesn't match an active class"). Valid rows have a checkbox (checked by default) to deselect before committing. A summary line up top: "42 valid, 3 errors, across 4 classes."
3. **Confirmation** (redirect after `POST` commit) — "38 students added" (accounting for any deselected rows), with a link to the Student List filtered to just-imported students.

If the institution has no active Classes yet, step 1 redirects straight to Class Setup with an explanatory message instead of offering a template download that would just produce an empty workbook.

---

## Fee Structure Builder (`/fee-structures/add/`)

A form with a dynamic itemized-line formset (Django formset, not a JS-built dynamic form) — "Add another line" submits/reloads with an extra blank row via a formset management-form pattern, consistent with minimal-JS. Running total shown, recalculated on each page load/submit rather than live via JS.

Once `locked`, `FeeStructureUpdateView` renders the same template in a read-only state with a banner: *"This fee structure is locked because at least one payment has been recorded against it. To make a correction, adjust the individual student's assignment instead,"* linking directly to the relevant `FeeAssignmentAdjustView`.

---

## Payment Recording (`/payments/add/`)

Student lookup first (typeahead against `SearchSuggestView` — the one place vanilla JS does something beyond form basics, since typing a name and seeing a live-filtered dropdown genuinely needs it). Selecting a student loads their current balance and any existing credit inline before the amount is entered. If existing credit exists, an "Apply credit toward this payment" checkbox appears. Method selector: Cash / Transfer / POS. On submit, redirects straight to the Receipt view for the payment just recorded.

---

## Receipt View (`/receipts/<id>/`)

Uses `base_print.html` — no nav, no sidebar, just institution letterhead-style header (name, code, address if set), the receipt body, and a "Print" button that calls the browser's native print dialog (`window.print()` — the only other place JS is used, and it's a one-liner). No PDF generation.

---

## Reports Hub (`/reports/...`)

A landing page linking to each of the four report types, each its own filtered-table view with a CSV export link at the top. No charts/graphs in V1 — number tables only, matching the original concept's explicit "no charts beyond number cards" boundary.

---

## Mobile Responsiveness

All screens above need to hold up on a phone-width viewport — stat card rows collapse to a single column, tables become horizontally scrollable rather than squeezed, and the sidebar collapses to a hamburger toggle. Flagged as its own pass in `07_Implementation_Roadmap.md` Phase 9, not something to verify only at the end — check each screen as it's built, not retrofit at the last phase.

## Still Open

- Exact visual styling (colors, typography, spacing system) — this document specifies layout and behavior, not a visual design system. A short design-tokens pass (`static/css/tokens.css`) is scheduled as the first item of Phase 3 (CR-006), so screens aren't styled ad hoc as they're built.
