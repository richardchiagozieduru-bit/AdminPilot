"""
Attendance views: dashboard overview, unified monthly register, and action
endpoints for marking attendance and exporting CSV.

Role gating follows docs/04_Permission_Matrix.md via MODULE_ACCESS:
  - Owner, Administrator: full access to all classes
  - Staff (Form Teacher): access only to their assigned class
  - Bursar: read-only access
"""

import csv
import json
import logging
from datetime import date

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from academic.models import Class, ClassStatus, Session, Term
from core.mixins import RoleRequiredMixin, TenantScopedQuerysetMixin
from core.permissions import has_module_access

from .models import AttendanceStatus, ClassAttendanceRegister
from .services import (
    clear_daily_attendance_for_class,
    get_class_monthly_matrix,
    get_month_school_days,
    mark_all_present_for_class,
    save_daily_attendance_for_class,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Permission helper
# --------------------------------------------------------------------------- #
def _check_class_access(user, klass):
    """Staff (form teachers) can only access their assigned class.

    Owners and Administrators can access any class.
    Bursars are handled at the view level (read-only).
    """
    if user.role in ("Owner", "Administrator"):
        return True
    if user.role == "Staff":
        return klass.form_teacher_id == user.pk
    if user.role == "Bursar":
        return True  # read-only enforced by module_action="manage" on write views
    return False


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
class AttendanceDashboardView(RoleRequiredMixin, TemplateView):
    """`/attendance/` — school-wide attendance overview.

    Form Teachers see their class prominently at the top with a direct
    "Open Register" action. All users see the class grid showing today's
    completion status.
    """

    template_name = "attendance/dashboard.html"
    module = "attendance"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        institution_id = self.request.institution_id
        today = date.today()

        # All active classes with form teacher info
        classes = (
            Class.unscoped.filter(
                institution_id=institution_id,
                status=ClassStatus.ACTIVE,
            )
            .select_related("form_teacher")
            .order_by("order", "name")
        )

        # Today's registers
        todays_registers = {
            r.klass_id: r
            for r in ClassAttendanceRegister.unscoped.filter(
                institution_id=institution_id,
                date=today,
            )
        }

        # Build class cards
        my_class = None
        class_cards = []

        if user.role == "Staff":
            my_class = classes.filter(form_teacher=user).first()
            if my_class:
                register = todays_registers.get(my_class.id)
                class_cards.append({
                    "klass": my_class,
                    "register": register,
                    "completed": register is not None and register.total_students > 0,
                    "rate": register.attendance_rate if register else None,
                    "present": register.present_count if register else 0,
                    "total": register.total_students if register else 0,
                })
        else:
            for klass in classes:
                register = todays_registers.get(klass.id)
                class_cards.append({
                    "klass": klass,
                    "register": register,
                    "completed": register is not None and register.total_students > 0,
                    "rate": register.attendance_rate if register else None,
                    "present": register.present_count if register else 0,
                    "total": register.total_students if register else 0,
                })

        # Overall school rate today (for admins/overview)
        completed_registers = [
            r for r in todays_registers.values() if r.total_students > 0
        ]
        total_present = sum(
            r.present_count + r.late_count for r in completed_registers
        )
        total_students = sum(r.total_students for r in completed_registers)
        school_rate = (
            round(total_present / total_students * 100, 1)
            if total_students > 0
            else None
        )

        context.update({
            "today": today,
            "class_cards": class_cards,
            "my_class": my_class,
            "completed_count": len(completed_registers),
            "total_classes": classes.count(),
            "school_rate": school_rate,
            "can_manage": self.can_manage(),
        })
        return context


# --------------------------------------------------------------------------- #
# Unified Monthly Register
# --------------------------------------------------------------------------- #
class ClassMonthlyRegisterView(RoleRequiredMixin, TemplateView):
    """`/attendance/class/<class_id>/` — the Unified Monthly Interactive Sheet.

    Query params: ?year=YYYY&month=MM (defaults to current month).
    """

    template_name = "attendance/class_monthly_register.html"
    module = "attendance"

    def dispatch(self, request, *args, **kwargs):
        self.klass = get_object_or_404(
            Class.unscoped.filter(institution_id=request.institution_id),
            pk=kwargs["class_id"],
        )
        if not _check_class_access(request.user, self.klass):
            raise PermissionDenied("You do not have access to this class register.")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = date.today()
        year = int(self.request.GET.get("year", today.year))
        month = int(self.request.GET.get("month", today.month))

        active_date = None
        date_str = self.request.GET.get("date")
        if date_str:
            try:
                parsed = date.fromisoformat(date_str)
                if parsed.year == year and parsed.month == month:
                    active_date = parsed
            except ValueError:
                pass

        matrix = get_class_monthly_matrix(
            institution_id=self.request.institution_id,
            klass=self.klass,
            year=year,
            month=month,
            active_date=active_date,
        )
        active_date = matrix["active_date"]

        # Organize school days by week for the template
        school_days = matrix["school_days"]
        weeks = []
        current_week = []
        for d in school_days:
            iso_week = d.isocalendar()[1]
            if current_week and current_week[0].isocalendar()[1] != iso_week:
                weeks.append(current_week)
                current_week = []
            current_week.append(d)
        if current_week:
            weeks.append(current_week)

        # Month navigation
        if month == 1:
            prev_year, prev_month = year - 1, 12
        else:
            prev_year, prev_month = year, month - 1
        if month == 12:
            next_year, next_month = year + 1, 1
        else:
            next_year, next_month = year, month + 1

        # Prepare status choices for the template
        status_choices = [
            {"value": s.value, "label": s.label}
            for s in AttendanceStatus
        ]

        from django.core.paginator import Paginator

        all_students = matrix["students"]
        paginator = Paginator(all_students, 20)
        page_number = self.request.GET.get("page", 1)
        page_obj = paginator.get_page(page_number)

        context.update({
            "klass": self.klass,
            "year": year,
            "month": month,
            "month_name": date(year, month, 1).strftime("%B %Y"),
            "today": today,
            "active_date": active_date,
            "current_school_day": matrix.get("current_school_day", today),
            "school_days": school_days,
            "weeks": weeks,
            "student_rows": page_obj,
            "page_obj": page_obj,
            "paginator": paginator,
            "total_students_count": len(all_students),
            "daily_totals": matrix["daily_totals"],
            "daily_totals_list": matrix["daily_totals_list"],
            "registers": matrix["registers"],
            "current_session": matrix["current_session"],
            "current_term": matrix["current_term"],
            "prev_year": prev_year,
            "prev_month": prev_month,
            "next_year": next_year,
            "next_month": next_month,
            "status_choices": status_choices,
            "can_manage": self.can_manage(),
            "is_current_month": year == today.year and month == today.month,
        })
        return context


# --------------------------------------------------------------------------- #
# Mark Today All Present
# --------------------------------------------------------------------------- #
class MarkTodayAllPresentView(RoleRequiredMixin, View):
    """`POST /attendance/class/<class_id>/mark-all-present/`"""

    module = "attendance"
    module_action = "manage"

    def post(self, request, class_id):
        klass = get_object_or_404(
            Class.unscoped.filter(institution_id=request.institution_id),
            pk=class_id,
        )
        if not _check_class_access(request.user, klass):
            raise PermissionDenied

        date_str = request.POST.get("date", "")
        try:
            target_date = date.fromisoformat(date_str) if date_str else date.today()
        except ValueError:
            target_date = date.today()

        try:
            register = mark_all_present_for_class(
                institution_id=request.institution_id,
                klass=klass,
                date_obj=target_date,
                actor=request.user,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(
                request,
                f"All {register.total_students} students marked present for "
                f"{klass.name} on {target_date}.",
            )
        except Exception:
            logger.exception("Mark all present failed for class %s", class_id)
            messages.error(request, "Could not mark all students present. Please try again.")

        redirect_url = reverse("attendance:class_register", kwargs={"class_id": class_id})
        page = request.POST.get("page", "")
        page_param = f"&page={page}" if page else ""
        return redirect(
            f"{redirect_url}?year={target_date.year}&month={target_date.month}&date={target_date.isoformat()}{page_param}"
        )


# --------------------------------------------------------------------------- #
# Clear / Unmark All Daily Attendance
# --------------------------------------------------------------------------- #
class ClearDailyAttendanceView(RoleRequiredMixin, View):
    """`POST /attendance/class/<class_id>/unmark-all/`"""

    module = "attendance"
    module_action = "manage"

    def post(self, request, class_id):
        klass = get_object_or_404(
            Class.unscoped.filter(institution_id=request.institution_id),
            pk=class_id,
        )
        if not _check_class_access(request.user, klass):
            raise PermissionDenied

        date_str = request.POST.get("date", "")
        try:
            target_date = date.fromisoformat(date_str) if date_str else date.today()
        except ValueError:
            target_date = date.today()

        try:
            clear_daily_attendance_for_class(
                institution_id=request.institution_id,
                klass=klass,
                date_obj=target_date,
                actor=request.user,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(
                request,
                f"Attendance cleared/unmarked for {klass.name} on {target_date}.",
            )
        except Exception:
            logger.exception("Clear attendance failed for class %s", class_id)
            messages.error(request, "Could not unmark attendance. Please try again.")

        redirect_url = reverse("attendance:class_register", kwargs={"class_id": class_id})
        page = request.POST.get("page", "")
        page_param = f"&page={page}" if page else ""
        return redirect(
            f"{redirect_url}?year={target_date.year}&month={target_date.month}&date={target_date.isoformat()}{page_param}"
        )


# --------------------------------------------------------------------------- #
# Save Daily Register
# --------------------------------------------------------------------------- #
class SaveDailyRegisterView(RoleRequiredMixin, View):
    """`POST /attendance/class/<class_id>/save-daily/`

    Accepts JSON body with records_data and the target date.
    """

    module = "attendance"
    module_action = "manage"

    def post(self, request, class_id):
        klass = get_object_or_404(
            Class.unscoped.filter(institution_id=request.institution_id),
            pk=class_id,
        )
        if not _check_class_access(request.user, klass):
            raise PermissionDenied

        # Support both JSON and form-encoded POST
        if request.content_type == "application/json":
            try:
                body = json.loads(request.body)
            except json.JSONDecodeError:
                return JsonResponse({"error": "Invalid JSON"}, status=400)
            date_str = body.get("date", "")
            records_data = body.get("records", [])
            notes = body.get("notes", "")
        else:
            date_str = request.POST.get("date", "")
            notes = request.POST.get("notes", "")
            # Parse form-encoded records: student_<id>=status
            records_data = []
            for key, value in request.POST.items():
                if key.startswith("student_") and value:
                    try:
                        student_id = int(key.replace("student_", ""))
                        remark = request.POST.get(f"remark_{student_id}", "")
                        records_data.append({
                            "student_id": student_id,
                            "status": value,
                            "remark": remark,
                        })
                    except (ValueError, TypeError):
                        continue

        try:
            target_date = date.fromisoformat(date_str) if date_str else date.today()
        except ValueError:
            target_date = date.today()

        if not records_data:
            if request.content_type == "application/json":
                return JsonResponse({"error": "No records provided"}, status=400)
            messages.error(request, "No attendance records to save.")
            return redirect("attendance:class_register", class_id=class_id)

        try:
            register = save_daily_attendance_for_class(
                institution_id=request.institution_id,
                klass=klass,
                date_obj=target_date,
                records_data=records_data,
                actor=request.user,
                notes=notes,
                ip_address=request.META.get("REMOTE_ADDR"),
            )

            if request.content_type == "application/json":
                return JsonResponse({
                    "status": "ok",
                    "present": register.present_count,
                    "absent": register.absent_count,
                    "late": register.late_count,
                    "total": register.total_students,
                    "rate": register.attendance_rate,
                })

            messages.success(
                request,
                f"Attendance saved for {klass.name} on {target_date} — "
                f"{register.attendance_rate}% present.",
            )
        except Exception:
            logger.exception("Save daily attendance failed for class %s", class_id)
            if request.content_type == "application/json":
                return JsonResponse({"error": "Save failed"}, status=500)
            messages.error(request, "Could not save attendance. Please try again.")

        redirect_url = reverse("attendance:class_register", kwargs={"class_id": class_id})
        page = request.POST.get("page", "")
        page_param = f"&page={page}" if page else ""
        return redirect(
            f"{redirect_url}?year={target_date.year}&month={target_date.month}&date={target_date.isoformat()}{page_param}"
        )


# --------------------------------------------------------------------------- #
# CSV Export
# --------------------------------------------------------------------------- #
class ClassAttendanceExportView(RoleRequiredMixin, View):
    """`GET /attendance/class/<class_id>/export/` — downloadable monthly CSV."""

    module = "attendance"

    def get(self, request, class_id):
        klass = get_object_or_404(
            Class.unscoped.filter(institution_id=request.institution_id),
            pk=class_id,
        )
        if not _check_class_access(request.user, klass):
            raise PermissionDenied

        today = date.today()
        year = int(request.GET.get("year", today.year))
        month = int(request.GET.get("month", today.month))

        matrix = get_class_monthly_matrix(
            institution_id=request.institution_id,
            klass=klass,
            year=year,
            month=month,
        )

        month_name = date(year, month, 1).strftime("%B_%Y")
        filename = f"Attendance_{klass.name}_{month_name}.csv"

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)

        # Header row
        header = ["#", "Student Name", "Admission No."]
        for d in matrix["school_days"]:
            header.append(d.strftime("%a %d"))
        header.extend(["Present", "Absent", "Late", "Rate %"])
        writer.writerow(header)

        # Student rows
        for idx, row in enumerate(matrix["students"], 1):
            student = row["student"]
            csv_row = [idx, student.full_name, student.admission_number]
            for cell in row["cells"]:
                if cell["status"]:
                    csv_row.append(cell["status_display"][0].upper())  # P/A/L/E
                else:
                    csv_row.append("-")
            totals = row["totals"]
            csv_row.extend([
                totals["present"],
                totals["absent"],
                totals["late"],
                f"{totals['rate']}%",
            ])
            writer.writerow(csv_row)

        # Daily totals row
        totals_row = ["", "Daily Total", ""]
        for d in matrix["school_days"]:
            dt = matrix["daily_totals"][d]
            totals_row.append(f"{dt['present']}P/{dt['absent']}A/{dt['late']}L")
        totals_row.extend(["", "", "", ""])
        writer.writerow(totals_row)

        return response
