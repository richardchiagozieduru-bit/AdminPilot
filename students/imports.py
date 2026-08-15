"""
Bulk student import: the three-step flow docs/03_Views_and_Endpoints.md lays out
— download a template, upload it, review a staged preview and commit.

Kept in its own module rather than in students/views.py because it is a
different shape of thing: a stateful pipeline across three requests with staging
tables between them, not the single-request CRUD next door. The actual parsing,
validation and student creation all live in students.services; these views are
the thin HTTP layer over it — generate/stream, hand the upload off to be staged,
render the staged rows, and commit the selected ones.

Nothing here writes to the `students` table. `stage_upload` fills the staging
tables and `commit_import` is the only thing that creates students, both in
services, both inside their own transaction.
"""

import logging
from io import BytesIO

from django.contrib import messages
from django.db import DatabaseError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import FormView, View

from academic.models import Class, ClassStatus, Term
from core.mixins import RoleRequiredMixin

from .forms import BulkImportUploadForm
from .models import (
    BulkImportBatch,
    BulkImportRow,
    BulkImportStatus,
    RowValidationStatus,
)
from .services import (
    BulkImportError,
    build_import_template,
    commit_import,
    stage_upload,
)

logger = logging.getLogger(__name__)

# The one MIME type for a modern .xlsx workbook. Named once so the download
# view and any future export agree on it.
XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


class BulkImportTemplateView(RoleRequiredMixin, View):
    """`GET /students/import/template/` — stream a fresh multi-sheet workbook.

    Built from the current active-class list every time (docs/03), never a
    static file: a class added this morning gets a worksheet this afternoon, and
    a deactivated one drops out. With no active classes there is nothing to sheet
    — the doc says redirect to class setup with a message rather than hand back
    an empty workbook that would only fail validation on the way back up.
    """

    module = "bulk_import"
    module_action = "view"

    def get(self, request, *args, **kwargs):
        if not Class.objects.filter(status=ClassStatus.ACTIVE).exists():
            messages.error(
                request,
                "Add at least one active class before downloading the import "
                "template — every class becomes a worksheet in it.",
            )
            return redirect("academic:class_list")

        institution = request.user.institution
        workbook = build_import_template(institution)

        # openpyxl writes to any file-like object; a BytesIO keeps the whole
        # thing in memory (a class list is small) and out of a temp file.
        buffer = BytesIO()
        workbook.save(buffer)

        response = HttpResponse(buffer.getvalue(), content_type=XLSX_CONTENT_TYPE)
        filename = f"{institution.code}_student_import_template.xlsx"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class BulkImportUploadView(RoleRequiredMixin, FormView):
    """`/students/import/` — pick a term, upload the filled-in template, stage it.

    On a valid upload the workbook is parsed and staged by services.stage_upload
    and the user is sent to the preview; nothing reaches `students` here. A file
    that cannot be read as .xlsx comes back as a field error (BulkImportError),
    not a 500.
    """

    module = "bulk_import"
    module_action = "manage"
    template_name = "students/import_upload.html"
    form_class = BulkImportUploadForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["institution_id"] = self.request.institution_id
        kwargs["current_term"] = Term.objects.filter(is_current=True).first()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # The page explains the flow and links to the template download; if no
        # classes exist yet, the template download would only bounce, so the
        # page says so up front instead.
        context["has_active_classes"] = Class.objects.filter(
            status=ClassStatus.ACTIVE
        ).exists()
        return context

    def form_valid(self, form):
        term = form.cleaned_data["term"]
        try:
            batch = stage_upload(
                institution=self.request.user.institution,
                uploaded_file=form.cleaned_data["file"],
                session=term.session,
                term=term,
                uploaded_by=self.request.user,
                ip_address=self.request.META.get("REMOTE_ADDR"),
            )
        except BulkImportError as exc:
            # The file was unreadable as a whole — a per-file problem, so it
            # belongs on the file field, not as a page-level error.
            form.add_error("file", str(exc))
            return self.form_invalid(form)
        except DatabaseError:
            logger.exception("Bulk import staging failed")
            messages.error(
                self.request,
                "Something went wrong reading that file. Please try again.",
            )
            return self.form_invalid(form)

        return redirect("students:import_preview", batch_id=batch.pk)


class BulkImportPreviewView(RoleRequiredMixin, View):
    """`/students/import/<batch_id>/preview/` — review staged rows, then commit.

    GET renders every staged row grouped by worksheet, each tagged valid or
    error-with-reason. POST reads the checkboxes (a manager may deselect valid
    rows), records the selection on the staging rows, and hands off to
    services.commit_import — which is the only place students are created, does
    it in one transaction, writes a single summarizing audit entry, and clears
    the staging rows afterward.
    """

    module = "bulk_import"
    module_action = "manage"
    template_name = "students/import_preview.html"

    def get_batch(self, request, batch_id):
        # Scoped queryset → a batch from another tenant is a 404, not a 403.
        return get_object_or_404(
            BulkImportBatch.objects.filter(institution_id=request.institution_id),
            pk=batch_id,
        )

    def get(self, request, batch_id):
        batch = self.get_batch(request, batch_id)
        if batch.status == BulkImportStatus.COMMITTED:
            messages.info(request, "That import has already been committed.")
            return redirect("students:list")

        rows = list(
            BulkImportRow.objects.filter(batch=batch).order_by(
                "sheet_name", "row_number"
            )
        )
        valid_count = sum(
            1 for row in rows if row.validation_status == RowValidationStatus.VALID
        )
        return render(
            request,
            self.template_name,
            {
                "batch": batch,
                "rows": rows,  # template regroups by sheet_name
                "valid_count": valid_count,
                "error_count": len(rows) - valid_count,
                "total_count": len(rows),
            },
        )

    def post(self, request, batch_id):
        batch = self.get_batch(request, batch_id)
        if batch.status == BulkImportStatus.COMMITTED:
            messages.info(request, "That import has already been committed.")
            return redirect("students:list")

        # Persist the manager's selection onto the valid rows before committing.
        # commit_import reads selected_for_commit, so this is what makes the
        # checkboxes authoritative. Only digit ids are trusted; anything else in
        # the POST is ignored rather than raising.
        selected_ids = [pk for pk in request.POST.getlist("rows") if pk.isdigit()]
        valid = BulkImportRow.objects.filter(
            batch=batch, validation_status=RowValidationStatus.VALID
        )
        valid.exclude(pk__in=selected_ids).update(selected_for_commit=False)
        valid.filter(pk__in=selected_ids).update(selected_for_commit=True)

        try:
            created = commit_import(
                batch=batch,
                actor=request.user,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
        except DatabaseError:
            logger.exception("Bulk import commit failed for batch %s", batch.pk)
            messages.error(
                request,
                "Something went wrong importing those students. Please try again.",
            )
            return redirect("students:import_preview", batch_id=batch.pk)

        if created:
            messages.success(
                request,
                f"Imported {created} student{'' if created == 1 else 's'}.",
            )
        else:
            messages.info(
                request,
                "No students were imported — none were selected on the preview.",
            )
        return redirect("students:list")
