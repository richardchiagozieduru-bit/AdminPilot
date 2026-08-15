# AdminPilot — V1 Scope Boundary (Django/MSSQL Port)

Single authoritative answer to "is this in V1" for this port. Derived from the original blueprint's `V1_Scope_Boundary.md`, with deliberate divergences — see the section at the bottom before assuming this matches the original document.

## In Scope — V1

**Auth & Access**
- Self-serve institution registration (school name mandatory, plus basic owner/contact details) — **diverges from the original blueprint**, see Divergences
- Manual approval by Super Admin before an institution's Owner account activates
- Login, forgot/reset password — Django's own auth, no external provider
- Single shared login page, no subdomain-based routing
- Roles: Owner, Administrator, Bursar, Staff (Staff inert — no active permissions yet), plus platform-level Super Admin (not institution-scoped)
- Field-level Bursar restrictions on student data

**Institution Setup**
- First-run wizard: institution details (name, code, type, timezone), first Session/Term, initial Classes
- Ongoing Session/Term creation
- Institution code editable until first receipt is issued, then permanently locked

**Dashboard**
- Stat cards, Action Required section, Quick Actions, Recent Transactions (content matches the original PDF concept doc's wireframe)

**Global Search**
- Cross-entity search: Students, Payments, Receipts

**Student Management**
- Full CRUD, required/optional field split, archive/reactivate
- Payment History, Enrollment History, Activity Timeline tabs
- **Bulk import via CSV/Excel — back in V1 scope.** The original blueprint dropped this because its single pilot school (Permrinech) is paper-only with nothing to import. That reasoning no longer holds now that self-serve registration means any school can sign up, including ones with an existing digital roster. **Format decided: multi-worksheet Excel, one tab per class**, tab name matching an active Class exactly — requires Class Setup to be completed first, since the downloadable template is generated dynamically from the institution's current class list. Full data model in `02_Database.md`; view flow in `03_Views_and_Endpoints.md`.
- Admission numbers are platform-generated (`{Code}-{Year}-{Sequence}`), not school-supplied

**Class Management**
- Full CRUD, never hard-deleted, deactivation allowed even with currently-enrolled students, blocks new enrollments only

**Fee Management**
- Itemized Fee Structures per class/term, auto-assignment to enrolled students (bidirectional — also triggered when a student enrolls after the structure already exists), individual waivers/adjustments
- Locks once any payment exists against it; corrections after that point via per-student adjustment only, reason required

**Payment Management**
- Record payments (cash/transfer/POS), partial/installment support
- Overpayments become student credit, applied manually by Bursar only — never auto-applied
- Payment reversal with mandatory reason
- Receipts: auto-generated, institution-based numbering (`{Code}-{Year}-{Sequence}`), browser print — no PDF/letterhead in V1

**Reports**
- Daily/Weekly/Monthly/Term Income, Outstanding Fees, Student Payment History, Class Payment Summary
- Timezone-aware date boundaries (per-institution configurable timezone, defaulting to `Africa/Lagos`)
- CSV export per report

**Settings**
- User management (invite/disable Administrator/Bursar)
- Academic Structure management
- Data Export (Students, Payments — CSV, on-demand, Owner-only)
- Audit Log (tiered visibility: Owner sees all, others see own actions only)

**Platform**
- Multi-tenant isolation via MSSQL Security Policies on every tenant table
- Super Admin registration-approval panel (New — see Divergences)
- Mobile-responsive across all screens

## Explicitly Out of Scope — Deferred to V2+

| Item | Why deferred |
|---|---|
| Bulk student promotion workflow | Schema supports it (StudentEnrollment); one-at-a-time via Edit Student is adequate at this scale |
| Itemized payment splitting (`payment_allocations`) | No V1 report requires item-level allocation |
| Multi-institution users / multi-campus under one Owner | Real architectural change, not a retrofit — route through a CR if needed |
| Payment gateway integration | Payment recording stays manual entry of transactions that happened offline |
| Automatic late fees/penalties | "Owing >30 days" is an informational dashboard flag only |
| Proof-of-payment attachment (photo/scan on a payment record) | Deferred — file/document upload isn't built yet for anything in V1 |
| PDF receipts with letterhead | Browser print is sufficient for V1; reasonable fast-follow CR |
| Active Staff/Teacher/Parent accounts | Role exists in schema, but no V1 screens grant access |
| SMS/email receipt delivery | Manual share (print) covers V1 |
| Branding/visual customization per school | Uniform look for all schools in V1 |
| Subdomain-based institution resolution | Single login page is sufficient; subdomains add infrastructure without doing any isolation work — see `01_Architecture.md` |
| Student self-service data entry section | Considered during planning, explicitly not built — any future work here needs its own CR given the child-data handling involved |
| All future modules from the original Long-Term Vision (Attendance, Academic Results, Payroll, Timetable, Inventory, Library, Transport, Hostel, Multi-campus, Analytics/BI, AI Assistant) | V1 stays scoped to Student Records + Fee Management only |

## Governance carried forward

- School = data controller, AdminPilot = data processor — to be stated explicitly in Terms of Service
- 90-day data retention after an institution cancels/lapses, then deletion
- Any change to this document's In Scope / Out of Scope lists is itself a scope decision — log it, don't drift into it mid-build

---

## Divergences from the Original Blueprint (read this before assuming parity)

The original blueprint (Next.js/Supabase, Permrinech-only pilot) made several V1 scope calls that **this port deliberately reverses or changes**:

1. **Registration model reversed.** Original: white-glove only, no public registration, self-serve explicitly deferred to V2. This port: self-serve registration **is** in V1, gated by manual Super Admin approval. This was a conscious product decision, not an oversight — made explicitly during planning for this port.
2. **Bulk import reinstated.** Original: dropped from V1 because the single pilot school had nothing to import. This port: reinstated, because self-serve registration means the "every school is paper-only" assumption no longer holds.
3. **New role added.** Super Admin doesn't exist anywhere in the original blueprint's Permission Matrix — it's new schema/permission surface required specifically by reversing decision #1.
4. **Subdomain routing was considered and rejected for this port too** — same outcome as the original blueprint reached, but arrived at independently, for a slightly different reason (no isolation value, not "no pilot-stage need").

Everything **not** listed above (roles, fee/payment logic, receipt numbering, timezone handling, governance) is inherited from the original blueprint's decisions as-is.
