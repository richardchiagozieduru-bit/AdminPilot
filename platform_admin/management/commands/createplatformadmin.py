"""
Alias command for createsuperuser.
Allows running `python manage.py createplatformadmin`.
"""

from .createsuperuser import Command as CreateSuperUserCommand


class Command(CreateSuperUserCommand):
    help = "Creates a platform-level Super Admin (PlatformUser) account (alias for createsuperuser)."
