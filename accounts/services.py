"""
Registration and user invitation services.

Handles atomic institution registration and owner creation, staff user invites
(CR-003 manual setup link generation), account activation, and role/status updates.
"""

from django.contrib.auth.tokens import default_token_generator
from django.db import transaction
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from core.middleware import institution_db_context
from core.models import Institution
from core.services import write_audit_log

from .models import User


def _derive_code(name, existing_codes):
    words = [w for w in name.upper().split() if w[:1].isalnum()]
    base = "".join(w[0] for w in words[:3]) if words else ""
    if len(base) < 3:
        base = "".join(ch for ch in name.upper() if ch.isalnum())[:4]
    base = (base or "SCH")[:12]

    if base not in existing_codes:
        return base
    for suffix in range(2, 1000):
        candidate = f"{base}{suffix}"
        if candidate not in existing_codes:
            return candidate
    raise RuntimeError(f"Could not derive a unique institution code from {name!r}.")


@transaction.atomic
def register_institution(
    *, school_name, school_type, owner_name, owner_email, owner_phone, password
):
    existing_codes = set(Institution.objects.values_list("code", flat=True))

    institution = Institution.objects.create(
        name=school_name,
        code=_derive_code(school_name, existing_codes),
        type=school_type,
        status=Institution.Status.PENDING,
        email=owner_email,
        phone=owner_phone,
    )

    with institution_db_context(institution.pk):
        User.objects.create_owner(
            email=owner_email,
            institution=institution,
            password=password,
            full_name=owner_name,
            phone=owner_phone,
        )

    return institution


# --------------------------------------------------------------------------- #
# Staff User Management (CR-003)
# --------------------------------------------------------------------------- #
@transaction.atomic
def invite_user(
    *, institution_id, full_name, email, role, actor, ip_address=None
):
    """Invite a new Administrator or Bursar staff member. CR-003.

    Creates an unactivated User (`is_active=False`). Returns (user, uidb64, token)
    so the view can render the setup link for manual sharing.
    """
    institution = Institution.objects.get(pk=institution_id)

    with institution_db_context(institution_id):
        user = User.objects.create_user(
            email=email,
            institution=institution,
            role=role,
            full_name=full_name,
            password=None,  # Unusable password until link accepted
            is_active=False,
        )

    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)

    write_audit_log(
        institution_id=institution_id,
        actor=actor,
        action="user.invited",
        summary=f"Invited {full_name} ({email}) as {role}",
        target_type="User",
        target_id=str(user.pk),
        detail={"email": email, "role": role},
        ip_address=ip_address,
    )

    return user, uidb64, token


@transaction.atomic
def activate_invited_user(*, user, password, ip_address=None):
    """Activate an invited staff member's account with their chosen password."""
    with institution_db_context(user.institution_id):
        user.set_password(password)
        user.is_active = True
        user.save(update_fields=["password", "is_active"])

        write_audit_log(
            institution_id=user.institution_id,
            actor=user,
            action="user.activated",
            summary=f"Activated account for {user.full_name}",
            target_type="User",
            target_id=str(user.pk),
            ip_address=ip_address,
        )
    return user


@transaction.atomic
def update_user_role_and_status(
    *, user, role, is_active, actor, ip_address=None
):
    """Update a staff member's role or toggle active status (Owner only)."""
    old_role = user.role
    old_status = user.is_active

    with institution_db_context(user.institution_id):
        user.role = role
        user.is_active = is_active
        user.save(update_fields=["role", "is_active"])

        action = "user.updated" if is_active else "user.disabled"
        write_audit_log(
            institution_id=user.institution_id,
            actor=actor,
            action=action,
            summary=f"Updated {user.full_name}'s account (role: {role}, active: {is_active})",
            target_type="User",
            target_id=str(user.pk),
            detail={
                "old_role": old_role,
                "new_role": role,
                "old_status": old_status,
                "new_status": is_active,
            },
            ip_address=ip_address,
        )
    return user
