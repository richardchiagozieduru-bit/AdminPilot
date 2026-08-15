"""
The permission matrix, as code.

docs/04_Permission_Matrix.md is authoritative and says the rule must live in
one place rather than as `if` checks scattered per view. This module is that
place: MODULE_ACCESS below is a direct transcription of that document's Module
Permissions grid, and every gated view reads it through the mixins here.

Role strings are duplicated from accounts.User.Role rather than imported.
`core` is the app nothing else's models depend on (docs/01_Architecture.md
ADR-003), and importing accounts here would invert that. The duplication is
guarded by a test that asserts the two lists still agree — drift fails the
suite instead of quietly granting access.

Two actions per module, because the grid distinguishes them: Bursar can *view*
students and classes but not change them. `view` gates read screens and drives
the sidebar; `manage` gates anything that mutates.
"""

from dataclasses import dataclass

OWNER = "Owner"
ADMINISTRATOR = "Administrator"
BURSAR = "Bursar"
# Inert in V1 by design: present in the enum so a future Teacher/Parent module
# needs no migration, but it appears in no `view` or `manage` set below.
STAFF = "Staff"

ALL_ROLES = (OWNER, ADMINISTRATOR, BURSAR, STAFF)

FULL = frozenset({OWNER, ADMINISTRATOR, BURSAR})
OWNER_ADMIN = frozenset({OWNER, ADMINISTRATOR})
OWNER_ONLY = frozenset({OWNER})
NOBODY = frozenset()


@dataclass(frozen=True)
class Module:
    """One row of the Module Permissions grid.

    `label` and `url_name` exist so the sidebar can be built from this same
    table — docs/08_UI_UX.md requires role filtering to happen server-side, and
    a nav built from anything other than the permission source would be able to
    disagree with it. url_name None means the module is reachable but not a
    top-level nav entry.
    """

    label: str
    view: frozenset
    manage: frozenset
    url_name: str = ""

    def allows(self, role, action="view"):
        return role in (self.manage if action == "manage" else self.view)


# Transcribed from docs/04_Permission_Matrix.md. Changing anything here is a
# permission-model change and goes through docs/06_CR_Process.md, not a quiet
# edit — the same rule that document states about itself.
MODULE_ACCESS = {
    "dashboard": Module("Dashboard", FULL, NOBODY, "core:dashboard"),
    "students": Module("Students", FULL, OWNER_ADMIN, "students:list"),
    "bulk_import": Module("Bulk Import", OWNER_ADMIN, OWNER_ADMIN),
    "classes": Module("Classes", FULL, OWNER_ADMIN, "academic:class_list"),
    "fee_structures": Module("Fee Structures", FULL, FULL, "billing:fee_structure_list"),
    "payments": Module("Payments", FULL, FULL, "billing:payment_list"),
    "receipts": Module("Receipts", FULL, FULL),
    "reports": Module("Reports", FULL, FULL, "reports:hub"),
    "search": Module("Search", FULL, NOBODY),
    "users": Module("Users", OWNER_ONLY, OWNER_ONLY, "accounts:user_list"),
    "institution_settings": Module(
        "Institution Settings", OWNER_ONLY, OWNER_ONLY, "core:institution_settings"
    ),
    "academic_structure": Module(
        "Academic Structure", OWNER_ONLY, OWNER_ONLY, "academic:structure"
    ),
    # Every role reads its own actions; only Owner sees the whole institution's.
    # The tiering is a queryset concern, handled in the view.
    "audit_log": Module("Audit Log", FULL, NOBODY, "core:audit_log"),
    "data_export": Module("Data Export", OWNER_ONLY, OWNER_ONLY),
}


def has_module_access(user, module, action="view"):
    """Single answer to 'may this user do this?'.

    A PlatformUser reaching here is a bug, not a permission question: platform
    accounts have no role and no institution, and every module in the grid is
    institution-scoped. Returning False keeps it a closed door rather than an
    AttributeError.
    """
    role = getattr(user, "role", None)
    if role is None:
        return False
    try:
        return MODULE_ACCESS[module].allows(role, action)
    except KeyError as exc:
        raise KeyError(
            f"Unknown module {module!r}. Add it to MODULE_ACCESS (and to "
            f"docs/04_Permission_Matrix.md first)."
        ) from exc


def visible_modules(user):
    """Nav entries this user may see, in grid order.

    Used by the context processor behind base.html's sidebar. A Bursar's page
    never contains User Management markup at all, per docs/08_UI_UX.md — the
    entry is absent from the context, not hidden with CSS.
    """
    role = getattr(user, "role", None)
    if role is None:
        return []
    return [
        module
        for module in MODULE_ACCESS.values()
        if module.url_name and role in module.view
    ]
