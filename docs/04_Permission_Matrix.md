# AdminPilot — Permission Matrix (v1.0)

**Status:** Authoritative. Companion to `03_Views_and_Endpoints.md` — every `Access` column in that document refers back to this one.

## Roles

| Role | Purpose |
|---|---|
| **Owner** | Ultimate authority over the institution's account; oversight, not daily data entry |
| **Administrator** | Full day-to-day operational control |
| **Bursar** | Financial modules only, with restricted student data access |
| **Staff** | Placeholder role for future modules (attendance, grading); no access in V1 |
| **Super Admin** | Platform-level (AdminPilot staff), not institution-scoped. Registration approval only — see `01_Architecture.md` |

One Owner is created automatically per institution on approval (`01_Architecture.md`'s registration flow). Owners invite Administrators and Bursars via `03_Views_and_Endpoints.md`'s `UserInviteView`.

## Module Permissions

| Module | Owner | Administrator | Bursar | Staff |
|---|---|---|---|---|
| Dashboard (view) | Full | Full | Financial widgets only | None |
| Student Management (CRUD) | Full | Full | View only, restricted fields (see below) | None |
| Bulk Import | Full | Full | None | None |
| Class Management (CRUD) | Full | Full | View only | None |
| Fee Structure (CRUD) | Full | Full | Full | None |
| Payment Recording | Full | Full | Full | None |
| Payment Reversal | Full (reason required) | Full (reason required) | Full (reason required) | None |
| Receipt Generation | Auto (view/reprint) | Auto (view/reprint) | Auto (view/reprint) | None |
| Outstanding Fee Tracking | Full | Full | Full | None |
| Reports (generate/export) | Full | Full | Financial reports only | None |
| Global Search | Full | Full | Restricted student fields | None |
| User Management (invite/disable) | Full | None | None | None |
| Institution Settings | Full | None | None | None |
| Academic Structure (Session/Term) | Full | None | None | None |
| Audit Log (view) | All institution activity | Own actions only | Own actions only | None |
| Data Export (Students/Payments) | Full | None | None | None |

Super Admin sits outside this grid — it's not an institution-scoped role and has no row here. Its only actions are approving/rejecting pending Institution registrations (`03_Views_and_Endpoints.md`'s Super Admin section). No code path exists from Super Admin into any row of this table.

## Bursar — Student Data Field-Level Access

Bursar has full access to the Payment and Fee modules but **restricted, field-level** access to Student records — enforced at the form/queryset layer, not just hidden in the template (`01_Architecture.md`'s View Layer Pattern).

**Bursar CAN access:**
- Student name
- Admission number
- Class
- Fee structure
- Balance
- Payment history
- Guardian phone number (payment-related communication only)

**Bursar CANNOT access:**
- Date of birth
- Home address
- Guardian email
- Father's/Mother's name (beyond whichever is on file as primary guardian contact)
- Any other non-financial personal field

**Implementation:** a restricted `ModelForm` (`Meta.fields` excludes the above) and a role-aware `get_queryset()`/context builder, used specifically when `request.user.role == 'Bursar'`. Defined once, reused everywhere a Bursar-role request touches student data — not re-implemented per view.

## Design Rationale

- **Owner vs. Administrator split** — Administrator cannot manage Users or Institution Settings, so an operational account can never lock out the Owner or alter billing/institution configuration.
- **Bursar is money-scoped, not people-scoped** — full control over fees/payments, minimal exposure to personal student data. Limits the blast radius if a Bursar account is ever compromised, and keeps the platform defensible under data-protection expectations as it scales.
- **Audit Log visibility is tiered** — Owner sees all institution activity; other roles see only their own actions.
- **Staff is inert on purpose** — present in the role enum now so a future Teacher/Parent module doesn't require a schema migration, but grants no access in V1.
- **Super Admin is structurally isolated, not just permission-gated** — a separate table/model (`02_Database.md`), not a role value that happens to have elevated permissions. This is deliberate: a permissions bug in the institution-scoped role system can't accidentally grant Super Admin-level access, because there's no shared code path to grant.

## Implementation Note

Enforced in Django via a `role_required` decorator/mixin applied to each view in `03_Views_and_Endpoints.md`, checking against this exact table — one place the rule is defined, not permission checks scattered per view. Any future change to this matrix (adding a module, changing a role's access) is a permission-model change and goes through the CR process (`06_CR_Process.md`) — not a quiet edit inside a view.
