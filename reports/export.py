"""
CSV export generators for reports and raw data export.

Outputs UTF-8 CSV with BOM (\\ufeff) header so files open cleanly in Microsoft Excel and Google Sheets.
"""

import csv

from django.http import HttpResponse

from billing.models import Payment, PaymentStatus
from students.models import Student


def create_csv_response(filename):
    """Build an HttpResponse configured for attachment download with UTF-8 BOM."""
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    # Write UTF-8 BOM for Excel compatibility
    response.write("\ufeff")
    return response


# --------------------------------------------------------------------------- #
# Report CSVs
# --------------------------------------------------------------------------- #
def generate_income_csv(report_data):
    filename = f"Income_Report_{report_data['date_from']}_to_{report_data['date_to']}.csv"
    response = create_csv_response(filename)
    writer = csv.writer(response)

    writer.writerow(["AdminPilot — Income Report"])
    writer.writerow(["Period", report_data["period"]])
    writer.writerow(["From", report_data["date_from"]])
    writer.writerow(["To", report_data["date_to"]])
    writer.writerow(["Total Collected", f"{report_data['total_collected']:.2f}"])
    writer.writerow(["Cash", f"{report_data['cash_total']:.2f}"])
    writer.writerow(["Transfer", f"{report_data['transfer_total']:.2f}"])
    writer.writerow(["POS", f"{report_data['pos_total']:.2f}"])
    writer.writerow([])

    writer.writerow(["Date", "Student", "Admission No", "Fee Structure", "Amount", "Method", "Receipt No", "Status"])
    for p in report_data["payments"]:
        receipt_num = p.receipt.receipt_number if hasattr(p, "receipt") and p.receipt else "—"
        writer.writerow([
            p.payment_date.strftime("%Y-%m-%d"),
            p.assignment.student.full_name,
            p.assignment.student.admission_number,
            p.assignment.fee_structure.name,
            f"{p.amount:.2f}",
            p.get_method_display(),
            receipt_num,
            p.get_status_display(),
        ])

    return response


def generate_outstanding_fees_csv(report_data):
    filename = "Outstanding_Fees_Report.csv"
    response = create_csv_response(filename)
    writer = csv.writer(response)

    writer.writerow(["AdminPilot — Outstanding Fees Report"])
    writer.writerow(["Total Due", f"{report_data['total_due']:.2f}"])
    writer.writerow(["Total Outstanding", f"{report_data['total_outstanding']:.2f}"])
    writer.writerow([])

    writer.writerow(["Student", "Admission No", "Class", "Term", "Fee Structure", "Amount Due", "Outstanding Balance"])
    for a in report_data["assignments"]:
        writer.writerow([
            a.student.full_name,
            a.student.admission_number,
            a.fee_structure.klass.name,
            a.fee_structure.term.name,
            a.fee_structure.name,
            f"{a.amount_due:.2f}",
            f"{a.outstanding_balance:.2f}",
        ])

    return response


def generate_student_payment_history_csv(report_data):
    student = report_data["student"]
    filename = f"Payment_History_{student.admission_number}.csv"
    response = create_csv_response(filename)
    writer = csv.writer(response)

    writer.writerow(["AdminPilot — Student Statement"])
    writer.writerow(["Student Name", student.full_name])
    writer.writerow(["Admission No", student.admission_number])
    writer.writerow(["Total Billed", f"{report_data['total_billed']:.2f}"])
    writer.writerow(["Total Paid", f"{report_data['total_paid']:.2f}"])
    writer.writerow(["Total Outstanding", f"{report_data['total_outstanding']:.2f}"])
    writer.writerow(["Credit Balance", f"{report_data['credit_balance']:.2f}"])
    writer.writerow([])

    writer.writerow(["FEE ASSIGNMENTS"])
    writer.writerow(["Fee Structure", "Class", "Term", "Amount Due", "Outstanding"])
    for a in report_data["assignments"]:
        writer.writerow([
            a.fee_structure.name,
            a.fee_structure.klass.name,
            a.fee_structure.term.name,
            f"{a.amount_due:.2f}",
            f"{a.outstanding_balance:.2f}",
        ])
    writer.writerow([])

    writer.writerow(["PAYMENTS"])
    writer.writerow(["Date", "Fee Structure", "Amount", "Method", "Receipt No", "Status"])
    for p in report_data["payments"]:
        receipt_num = p.receipt.receipt_number if hasattr(p, "receipt") and p.receipt else "—"
        writer.writerow([
            p.payment_date.strftime("%Y-%m-%d"),
            p.assignment.fee_structure.name,
            f"{p.amount:.2f}",
            p.get_method_display(),
            receipt_num,
            p.get_status_display(),
        ])

    return response


def generate_class_summary_csv(report_data):
    filename = "Class_Payment_Summary.csv"
    response = create_csv_response(filename)
    writer = csv.writer(response)

    writer.writerow(["AdminPilot — Class Payment Summary Report"])
    writer.writerow(["Grand Total Billed", f"{report_data['grand_billed']:.2f}"])
    writer.writerow(["Grand Total Collected", f"{report_data['grand_collected']:.2f}"])
    writer.writerow(["Grand Total Outstanding", f"{report_data['grand_outstanding']:.2f}"])
    writer.writerow([])

    writer.writerow(["Class", "Students", "Total Billed", "Total Collected", "Total Outstanding", "Fully Paid Students"])
    for row in report_data["summaries"]:
        writer.writerow([
            row["class"].name,
            row["student_count"],
            f"{row['total_billed']:.2f}",
            f"{row['total_collected']:.2f}",
            f"{row['total_outstanding']:.2f}",
            row["fully_paid_count"],
        ])

    return response


# --------------------------------------------------------------------------- #
# Owner Data Exports
# --------------------------------------------------------------------------- #
def generate_student_roster_csv(institution_id):
    filename = "Students_Roster_Export.csv"
    response = create_csv_response(filename)
    writer = csv.writer(response)

    writer.writerow([
        "Admission No", "First Name", "Middle Name", "Last Name", "Gender",
        "Date of Birth", "Guardian Name", "Guardian Phone", "Guardian Email",
        "Address", "Status", "Credit Balance"
    ])

    students = Student.unscoped.filter(
        institution_id=institution_id
    ).order_by("admission_number")

    for s in students:
        writer.writerow([
            s.admission_number,
            s.first_name,
            s.middle_name,
            s.last_name,
            s.get_gender_display(),
            s.date_of_birth.strftime("%Y-%m-%d") if s.date_of_birth else "",
            s.guardian_name,
            s.guardian_phone,
            s.guardian_email,
            s.address,
            s.get_status_display(),
            f"{s.credit_balance:.2f}",
        ])

    return response


def generate_payment_export_csv(institution_id, date_from=None, date_to=None):
    filename = "Payment_History_Export.csv"
    response = create_csv_response(filename)
    writer = csv.writer(response)

    writer.writerow([
        "Payment ID", "Payment Date", "Student Name", "Admission No",
        "Class", "Fee Structure", "Amount", "Method", "Receipt No",
        "Status", "Recorded By", "Recorded At"
    ])

    payments = Payment.unscoped.filter(
        institution_id=institution_id
    ).select_related(
        "assignment__student",
        "assignment__fee_structure",
        "assignment__fee_structure__klass",
        "receipt",
        "recorded_by",
    ).order_by("-payment_date", "-created_at")

    if date_from:
        payments = payments.filter(payment_date__gte=date_from)
    if date_to:
        payments = payments.filter(payment_date__lte=date_to)

    for p in payments:
        receipt_num = p.receipt.receipt_number if hasattr(p, "receipt") and p.receipt else "—"
        recorded_by = p.recorded_by.full_name if p.recorded_by else "System"
        writer.writerow([
            p.pk,
            p.payment_date.strftime("%Y-%m-%d"),
            p.assignment.student.full_name,
            p.assignment.student.admission_number,
            p.assignment.fee_structure.klass.name,
            p.assignment.fee_structure.name,
            f"{p.amount:.2f}",
            p.get_method_display(),
            receipt_num,
            p.get_status_display(),
            recorded_by,
            p.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        ])

    return response
