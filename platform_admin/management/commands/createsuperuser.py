"""
Management command to create a Platform Super Admin (PlatformUser).

Overrides Django's default `createsuperuser` command so `manage.py createsuperuser`
creates a platform-level Super Admin account instead of failing on the
institution-scoped accounts.User model.
"""

import getpass
import sys
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.contrib.auth.password_validation import validate_password

from platform_admin.models import PlatformUser


class Command(BaseCommand):
    help = "Creates a platform-level Super Admin (PlatformUser) account."

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            dest="email",
            default=None,
            help="Email address for the Super Admin account.",
        )
        parser.add_argument(
            "--full-name",
            dest="full_name",
            default="",
            help="Full name for the Super Admin.",
        )
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Do NOT prompt the user for input of any kind.",
        )

    def handle(self, *args, **options):
        email = options.get("email")
        full_name = options.get("full_name") or ""
        interactive = options.get("interactive", True)

        if not interactive:
            if not email:
                raise CommandError("--email is required in non-interactive mode.")
            email = email.strip().lower()
            try:
                validate_email(email)
            except ValidationError as e:
                raise CommandError(f"Invalid email: {e.message}")

            if PlatformUser.objects.filter(email__iexact=email).exists():
                raise CommandError(f"A PlatformUser with email '{email}' already exists.")

            password = options.get("password")
            if not password:
                raise CommandError("Password is required in non-interactive mode.")

            PlatformUser.objects.create_superuser(
                email=email,
                password=password,
                full_name=full_name,
            )
            self.stdout.write(
                self.style.SUCCESS(f"Super Admin account '{email}' created successfully.")
            )
            return

        # Interactive Mode
        self.stdout.write("Create a Platform Super Admin account\n")

        # 1. Prompt Email
        while not email:
            try:
                input_email = input("Email: ").strip()
            except (KeyboardInterrupt, EOFError):
                self.stderr.write("\nOperation cancelled.")
                return

            if not input_email:
                self.stderr.write("Error: Email cannot be blank.\n")
                continue

            try:
                validate_email(input_email)
            except ValidationError:
                self.stderr.write("Error: Enter a valid email address.\n")
                continue

            if PlatformUser.objects.filter(email__iexact=input_email).exists():
                self.stderr.write(f"Error: A Super Admin with email '{input_email}' already exists.\n")
                continue

            email = input_email.lower()

        # 2. Prompt Full Name (optional)
        if not full_name:
            try:
                full_name = input("Full name (optional): ").strip()
            except (KeyboardInterrupt, EOFError):
                self.stderr.write("\nOperation cancelled.")
                return

        # 3. Prompt Password
        password = None
        while not password:
            try:
                p1 = getpass.getpass("Password: ")
                if not p1:
                    self.stderr.write("Error: Blank passwords aren't allowed.\n")
                    continue
                p2 = getpass.getpass("Password (again): ")
            except (KeyboardInterrupt, EOFError):
                self.stderr.write("\nOperation cancelled.")
                return

            if p1 != p2:
                self.stderr.write("Error: Your passwords didn't match.\n")
                continue

            # Validate password
            fake_user = PlatformUser(email=email, full_name=full_name)
            try:
                validate_password(p1, user=fake_user)
            except ValidationError as err:
                self.stderr.write("Password validation warnings:\n")
                for msg in err.messages:
                    self.stderr.write(f" - {msg}\n")
                try:
                    bypass = input("Bypass password validation and create user anyway? [y/N]: ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    self.stderr.write("\nOperation cancelled.")
                    return
                if bypass != "y":
                    continue

            password = p1

        # Create Super Admin
        PlatformUser.objects.create_superuser(
            email=email,
            password=password,
            full_name=full_name,
        )

        self.stdout.write(
            self.style.SUCCESS(f"\nSuper Admin account '{email}' created successfully.")
        )
