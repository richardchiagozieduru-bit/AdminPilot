"""
Reports and Data Export views.

Module permissions:
  - Reports: Owner, Administrator, Bursar (full access per docs/04_Permission_Matrix.md)
  - Data Export: Owner only (writes audit log action='data.exported' per docs/03_Views_and_Endpoints.md)
"""

import logging

from django.shortcuts import get_object_or_404
from django.views.generic import TemplateView, View

from academic.models import Class, ClassStatus, Term
from core.mixins import RoleRequiredMixin, TenantScopedQuerysetMixin
from core.services import write_audit_log
from reports.export import (
    generate_class_summary_csv,
    generate_income_csv,
    generate_outstanding_fees_csv,
    generate_payment_export_csv,
    generate_student_payment_history_csv,
    generate_student_roster_csv,
)
from reports.services import (
    get_class_summary_data,
    get_income_report_data,
    get_outstanding_fees_data,
    get_student_payment_history_data,
)
from students.models import Student

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Reports Hub
# --------------------------------------------------------------------------- #
class ReportsHubView(RoleRequiredMixin, TemplateView):
    """`/reports/` — central hub linking to all reports and data export."""

    template_name = "reports/reports_hub.html"
    module = "reports"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_export"] = self.request.user.is_owner
        return context


# --------------------------------------------------------------------------- #
# Income Report
# --------------------------------------------------------------------------- #
class IncomeReportView(RoleRequiredMixin, TemplateView):
    """`/reports/income/` — date/period filterable income report."""

    template_name = "reports/income_report.html"
    module = "reports"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        period = self.request.GET.get("period", "term")
        date_from = self.request.GET.get("date_from", "")
        date_to = self.request.GET.get("date_to", "")
        term_id = self.request.GET.get("term_id", "")

        data = get_income_report_data(
            institution_id=self.request.institution_id,
            period=period,
            date_from=date_from if date_from else None,
            date_to=date_to if date_to else None,
            term_id=term_id if term_id else None,
        )

        context["report"] = data
        context["terms"] = Term.unscoped.filter(
            institution_id=self.request.institution_id
        ).order_by("session__start_date", "start_date")
        context["selected_period"] = period
        context["selected_date_from"] = date_from
        context["selected_date_to"] = date_to
        context["selected_term_id"] = term_id
        return context


class IncomeReportExportView(RoleRequiredMixin, View):
    """`/reports/income/export/?format=csv` — Income CSV export."""

    module = "reports"

    def get(self, request, *args, **kwargs):
        period = request.GET.get("period", "term")
        date_from = request.GET.get("date_from", "")
        date_to = request.GET.get("date_to", "")
        term_id = request.GET.get("term_id", "")

        data = get_income_report_data(
            institution_id=request.institution_id,
            period=period,
            date_from=date_from if date_from else None,
            date_to=date_to if date_to else None,
            term_id=term_id if term_id else None,
        )
        return generate_income_csv(data)


# --------------------------------------------------------------------------- #
# Outstanding Fees Report
# --------------------------------------------------------------------------- #
class OutstandingFeesReportView(RoleRequiredMixin, TemplateView):
    """`/reports/outstanding-fees/` — filterable outstanding fees report."""

    template_name = "reports/outstanding_fees_report.html"
    module = "reports"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        class_id = self.request.GET.get("class_id", "")
        term_id = self.request.GET.get("term_id", "")

        data = get_outstanding_fees_data(
            institution_id=self.request.institution_id,
            class_id=class_id if class_id else None,
            term_id=term_id if term_id else None,
        )

        context["report"] = data
        context["classes"] = Class.unscoped.filter(
            institution_id=self.request.institution_id,
            status=ClassStatus.ACTIVE,
        ).order_by("order", "name")
        context["terms"] = Term.unscoped.filter(
            institution_id=self.request.institution_id
        ).order_by("session__start_date", "start_date")
        return context


class OutstandingFeesExportView(RoleRequiredMixin, View):
    """`/reports/outstanding-fees/export/?format=csv` — Outstanding fees CSV export."""

    module = "reports"

    def get(self, request, *args, **kwargs):
        class_id = request.GET.get("class_id", "")
        term_id = request.GET.get("term_id", "")

        data = get_outstanding_fees_data(
            institution_id=request.institution_id,
            class_id=class_id if class_id else None,
            term_id=term_id if term_id else None,
        )
        return generate_outstanding_fees_csv(data)


# --------------------------------------------------------------------------- #
# Student Payment History Report
# --------------------------------------------------------------------------- #
class StudentPaymentHistoryReportView(RoleRequiredMixin, TemplateView):
    """`/reports/student/<id>/payment-history/` — single student billing statement."""

    template_name = "reports/student_payment_history_report.html"
    module = "reports"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        student_id = self.kwargs["pk"]
        data = get_student_payment_history_data(
            institution_id=self.request.institution_id,
            student_id=student_id,
        )
        context["report"] = data
        return context


class StudentPaymentHistoryExportView(RoleRequiredMixin, View):
    """`/reports/student/<id>/payment-history/export/?format=csv`."""

    module = "reports"

    def get(self, request, *args, **kwargs):
        student_id = kwargs["pk"]
        data = get_student_payment_history_data(
            institution_id=request.institution_id,
            student_id=student_id,
        )
        return generate_student_payment_history_csv(data)


# --------------------------------------------------------------------------- #
# Class Summary Report
# --------------------------------------------------------------------------- #
class ClassSummaryReportView(RoleRequiredMixin, TemplateView):
    """`/reports/class-summary/` — billing summary table per class."""

    template_name = "reports/class_summary_report.html"
    module = "reports"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        class_id = self.request.GET.get("class_id", "")
        term_id = self.request.GET.get("term_id", "")

        data = get_class_summary_data(
            institution_id=self.request.institution_id,
            class_id=class_id if class_id else None,
            term_id=term_id if term_id else None,
        )

        context["report"] = data
        context["classes"] = Class.unscoped.filter(
            institution_id=self.request.institution_id,
            status=ClassStatus.ACTIVE,
        ).order_by("order", "name")
        context["terms"] = Term.unscoped.filter(
            institution_id=self.request.institution_id
        ).order_by("session__start_date", "start_date")
        context["selected_class_id"] = class_id
        context["selected_term_id"] = term_id
        return context


class ClassSummaryExportView(RoleRequiredMixin, View):
    """`/reports/class-summary/export/?format=csv`."""

    module = "reports"

    def get(self, request, *args, **kwargs):
        class_id = request.GET.get("class_id", "")
        term_id = request.GET.get("term_id", "")

        data = get_class_summary_data(
            institution_id=request.institution_id,
            class_id=class_id if class_id else None,
            term_id=term_id if term_id else None,
        )
        return generate_class_summary_csv(data)


# --------------------------------------------------------------------------- #
# Data Export (Owner Only)
# --------------------------------------------------------------------------- #
class StudentExportView(RoleRequiredMixin, View):
    """`/export/students/` — Full student roster export (Owner only)."""

    module = "data_export"
    module_action = "manage"

    def get(self, request, *args, **kwargs):
        write_audit_log(
            institution_id=request.institution_id,
            actor=request.user,
            action="data.exported",
            summary="Exported full student roster CSV",
            target_type="DataExport",
            target_id=str(request.institution_id),
            detail={"type": "students"},
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        return generate_student_roster_csv(request.institution_id)


class PaymentExportView(RoleRequiredMixin, View):
    """`/export/payments/?date_from=&date_to=` — Full payment history export (Owner only)."""

    module = "data_export"
    module_action = "manage"

    def get(self, request, *args, **kwargs):
        date_from = request.GET.get("date_from", "")
        date_to = request.GET.get("date_to", "")

        write_audit_log(
            institution_id=request.institution_id,
            actor=request.user,
            action="data.exported",
            summary="Exported payment history CSV",
            target_type="DataExport",
            target_id=str(request.institution_id),
            detail={"type": "payments", "date_from": date_from, "date_to": date_to},
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        return generate_payment_export_csv(
            request.institution_id,
            date_from=date_from if date_from else None,
            date_to=date_to if date_to else None,
        )
