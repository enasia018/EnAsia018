import base64
import hashlib
import json
import math
import os
import re
import secrets
import time
import uuid
from datetime import date, datetime, timezone
from io import BytesIO
from urllib.parse import quote

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    render_template,
    request,
    session,
)
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import func, or_
from werkzeug.security import check_password_hash, generate_password_hash

office_db_bp = Blueprint("office_db", __name__, url_prefix="/admin/database")

SESSION_LIFETIME_MS = 12 * 60 * 60 * 1000
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
USER_ROLES = {"Admin", "Editor", "View only"}
PAYMENT_TYPES = {
    "Membership Fee",
    "Franchise Processing Fee",
    "Other Payment",
}
IMPORT_PAYMENT_TYPES = PAYMENT_TYPES | {"Monthly Dues"}
VALID_MEMBER_STATUSES = {"Active", "Inactive", "Pending"}
VALID_PA_CPC_STATUSES = {"Yes", "No", "Pending", "Not recorded"}

MEMBER_FIELDS = [
    "member_code",
    "full_name",
    "address",
    "id_number",
    "driver_license_number",
    "unit_name",
    "vehicle_make",
    "vehicle_model",
    "vehicle_year",
    "plate_number",
    "mv_file_number",
    "franchise_case_number",
    "batch_number",
    "franchise_start_date",
    "monthly_dues_amount",
    "due_day",
    "status",
    "moa_status",
    "lease_status",
    "pa_status",
    "cpc_status",
    "original_or_cr",
    "facebook_name",
    "terms",
    "notes",
]
TEXT_MEMBER_FIELDS = [
    f for f in MEMBER_FIELDS if f not in ("monthly_dues_amount", "due_day")
]

IMPORT_MEMBER_COLUMNS = {
    "Member Code": "member_code",
    "Full Name": "full_name",
    "Address": "address",
    "ID Number": "id_number",
    "Driver License Number": "driver_license_number",
    "Unit Name": "unit_name",
    "Vehicle Make": "vehicle_make",
    "Vehicle Model": "vehicle_model",
    "Vehicle Year": "vehicle_year",
    "Plate Number": "plate_number",
    "MV File Number": "mv_file_number",
    "Franchise Application Case Number": "franchise_case_number",
    "Batch Number": "batch_number",
    "Franchise Start Date": "franchise_start_date",
    "Monthly Dues Amount": "monthly_dues_amount",
    "Due Day (1-28)": "due_day",
    "Member Status": "status",
    "MOA Status": "moa_status",
    "Lease Status": "lease_status",
    "PA Status": "pa_status",
    "CPC Status": "cpc_status",
    "Original OR/CR": "original_or_cr",
    "Facebook Name": "facebook_name",
    "Terms": "terms",
    "Notes": "notes",
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,50}$")
BASE64_RE = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")
UUID_RE = re.compile(r"^[0-9a-fA-F-]+$")

_db = None
_models = {}
_portal_member_model = None
_login_attempts = {}


def _sync_office_member_to_portal(office_member, old_code=None, default_pw_hash=None, portal_by_code=None):
    """Automatically mirror an OfficeMember record into the Member Portal / Members Directory table."""
    if _portal_member_model is None or office_member is None:
        return
    PortalMember = _portal_member_model
    code = (office_member.member_code or "").strip()
    if not code:
        return

    portal_member = None
    if portal_by_code is not None:
        if old_code and old_code.strip().lower() != code.lower():
            portal_member = portal_by_code.get(old_code.strip().lower())
        if portal_member is None:
            portal_member = portal_by_code.get(code.lower())
    else:
        if old_code and old_code.strip().lower() != code.lower():
            portal_member = PortalMember.query.filter(
                func.lower(PortalMember.member_code) == old_code.strip().lower()
            ).first()
        if portal_member is None:
            portal_member = PortalMember.query.filter(
                func.lower(PortalMember.member_code) == code.lower()
            ).first()

    vehicle_parts = [
        p.strip()
        for p in (
            office_member.vehicle_make or "",
            office_member.vehicle_model or "",
            office_member.vehicle_year or "",
        )
        if p and p.strip()
    ]
    vehicle_str = " ".join(vehicle_parts) if vehicle_parts else None
    dues_val = float(office_member.monthly_dues_amount or 0.0)
    dues_str = f"₱{dues_val:,.2f}"
    due_day_str = str(office_member.due_day) if office_member.due_day is not None else None

    if portal_member is None:
        portal_member = PortalMember(
            member_code=code,
            full_name=(office_member.full_name or "").strip() or code,
            must_change_password=True,
        )
        if default_pw_hash:
            portal_member.password_hash = default_pw_hash
        else:
            portal_member.set_password("Password123")
        _db.session.add(portal_member)
        if portal_by_code is not None:
            portal_by_code[code.lower()] = portal_member

    portal_member.member_code = code
    portal_member.full_name = (office_member.full_name or "").strip() or portal_member.full_name
    portal_member.status = (office_member.status or "Active").strip() or "Active"
    portal_member.address = (office_member.address or "").strip() or None
    portal_member.member_id_no = (office_member.id_number or "").strip() or None
    portal_member.driver_license = (office_member.driver_license_number or "").strip() or None
    portal_member.unit_name = (office_member.unit_name or "").strip() or None
    if vehicle_str:
        portal_member.vehicle = vehicle_str
    portal_member.plate_number = (office_member.plate_number or "").strip() or None
    portal_member.mv_file_number = (office_member.mv_file_number or "").strip() or None
    portal_member.franchise_case = (office_member.franchise_case_number or "").strip() or None
    portal_member.batch_number = (office_member.batch_number or "").strip() or None
    portal_member.franchise_start_date = (office_member.franchise_start_date or "").strip() or None
    portal_member.monthly_dues = dues_str
    portal_member.due_day = due_day_str
    portal_member.moa_status = (office_member.moa_status or "").strip() or None
    portal_member.lease_status = (office_member.lease_status or "").strip() or None
    portal_member.pa_status = (office_member.pa_status or "Not recorded").strip() or "Not recorded"
    portal_member.cpc_status = (office_member.cpc_status or "Not recorded").strip() or "Not recorded"
    portal_member.original_or_cr = (office_member.original_or_cr or "").strip() or None
    portal_member.facebook_name = (office_member.facebook_name or "").strip() or None
    portal_member.terms = (office_member.terms or "").strip() or None


def sync_all_office_members_to_portal():
    """Ensure all OfficeMember records are synced into the Member Portal / Members Directory table in a single batch."""
    if _portal_member_model is None or "OfficeMember" not in _models:
        return
    OfficeMember = _models["OfficeMember"]
    PortalMember = _portal_member_model
    portal_by_code = {
        (pm.member_code or "").strip().lower(): pm
        for pm in PortalMember.query.all()
        if pm.member_code
    }
    default_pw_hash = generate_password_hash("Password123")
    for om in OfficeMember.query.all():
        _sync_office_member_to_portal(
            om, default_pw_hash=default_pw_hash, portal_by_code=portal_by_code
        )
    _db.session.commit()


def sync_portal_member_to_office(portal_member, old_code=None):
    """Also mirror any edits made in /admin/members back into OfficeMember so both stay identical."""
    if portal_member is None or "OfficeMember" not in _models:
        return
    OfficeMember = _models["OfficeMember"]
    code = (portal_member.member_code or "").strip()
    if not code:
        return

    om = None
    if old_code and old_code.strip().lower() != code.lower():
        om = OfficeMember.query.filter(
            func.lower(OfficeMember.member_code) == old_code.strip().lower()
        ).first()
    if om is None:
        om = OfficeMember.query.filter(
            func.lower(OfficeMember.member_code) == code.lower()
        ).first()

    ts = _now_iso()
    dues_raw = (portal_member.monthly_dues or "0").replace("₱", "").replace(",", "").strip()
    try:
        dues_num = max(float(dues_raw), 0.0)
    except ValueError:
        dues_num = 0.0

    due_day_num = None
    if portal_member.due_day:
        try:
            dd = int(str(portal_member.due_day).strip())
            if 1 <= dd <= 28:
                due_day_num = dd
        except ValueError:
            due_day_num = None

    if om is None:
        om = OfficeMember(
            id=str(uuid.uuid4()),
            member_code=code,
            full_name=(portal_member.full_name or "").strip() or code,
            created_at=ts,
            updated_at=ts,
        )
        _db.session.add(om)

    om.member_code = code
    om.full_name = (portal_member.full_name or "").strip() or om.full_name
    om.status = (portal_member.status or "Active") if (portal_member.status in VALID_MEMBER_STATUSES) else "Active"
    om.address = portal_member.address or ""
    om.id_number = portal_member.member_id_no or ""
    om.driver_license_number = portal_member.driver_license or ""
    om.unit_name = portal_member.unit_name or ""
    if portal_member.vehicle and not (om.vehicle_make or om.vehicle_model or om.vehicle_year):
        om.vehicle_model = portal_member.vehicle
    om.plate_number = portal_member.plate_number or ""
    om.mv_file_number = portal_member.mv_file_number or ""
    om.franchise_case_number = portal_member.franchise_case or ""
    om.batch_number = portal_member.batch_number or ""
    om.franchise_start_date = portal_member.franchise_start_date or ""
    om.monthly_dues_amount = dues_num
    om.due_day = due_day_num
    om.moa_status = portal_member.moa_status or ""
    om.lease_status = portal_member.lease_status or ""
    om.pa_status = (portal_member.pa_status or "Not recorded") if (portal_member.pa_status in VALID_PA_CPC_STATUSES) else "Not recorded"
    om.cpc_status = (portal_member.cpc_status or "Not recorded") if (portal_member.cpc_status in VALID_PA_CPC_STATUSES) else "Not recorded"
    om.original_or_cr = portal_member.original_or_cr or ""
    om.facebook_name = portal_member.facebook_name or ""
    om.terms = portal_member.terms or ""
    om.updated_at = ts


class ApiError(Exception):
    def __init__(self, message, status=400, extra=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra or {}


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _now_ms():
    return int(time.time() * 1000)


def _json_response(payload, status=200):
    resp = jsonify(payload)
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _validate_text(value, label, required=False, max_len=500):
    if not isinstance(value, str):
        raise ApiError(f"{label} must be text.", 400)
    clean = value.strip()
    if required and not clean:
        raise ApiError(f"{label} is required.", 400)
    if len(clean) > max_len:
        raise ApiError(f"{label} must be {max_len} characters or fewer.", 400)
    return clean


def _validate_date(value, label, required=False):
    raw = "" if value is None else value
    clean = _validate_text(raw, label, max_len=10)
    if not clean and required:
        raise ApiError(f"{label} is required.", 400)
    if clean:
        if not DATE_RE.match(clean):
            raise ApiError(f"{label} must use YYYY-MM-DD format.", 400)
        try:
            datetime.strptime(clean, "%Y-%m-%d")
        except ValueError:
            raise ApiError(f"{label} must be a valid calendar date (YYYY-MM-DD).", 400)
    return clean


def _parse_float(value, error_message):
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ApiError(error_message, 400)
    if not math.isfinite(num):
        raise ApiError(error_message, 400)
    return num


def _validate_member(data):
    if not isinstance(data, dict):
        raise ApiError("Member details are required.", 400)

    member = {}
    for field in TEXT_MEMBER_FIELDS:
        raw = data.get(field, "")
        if raw is None:
            raw = ""
        elif not isinstance(raw, str):
            raw = str(raw)
        member[field] = _validate_text(
            raw,
            field.replace("_", " "),
            required=(field == "full_name"),
            max_len=2000 if field in ("notes", "address") else 255,
        )

    member["franchise_start_date"] = _validate_date(
        data.get("franchise_start_date", "") or "",
        "Franchise start date",
    )

    raw_dues = data.get("monthly_dues_amount", 0)
    if raw_dues in ("", None):
        raw_dues = 0
    dues = _parse_float(raw_dues, "Monthly dues must be a non-negative amount.")
    if dues < 0 or dues > 1_000_000_000:
        raise ApiError("Monthly dues must be a non-negative amount.", 400)
    member["monthly_dues_amount"] = dues

    raw_due_day = data.get("due_day")
    if raw_due_day in ("", None):
        member["due_day"] = None
    else:
        due_day_float = _parse_float(raw_due_day, "Due day must be between 1 and 28.")
        if not due_day_float.is_integer() or int(due_day_float) < 1 or int(due_day_float) > 28:
            raise ApiError("Due day must be between 1 and 28.", 400)
        member["due_day"] = int(due_day_float)

    if member["status"] and member["status"] not in VALID_MEMBER_STATUSES:
        raise ApiError("Status must be Active, Inactive, or Pending.", 400)
    if not member["status"]:
        member["status"] = "Active"

    for field in ("pa_status", "cpc_status"):
        if not member[field]:
            member[field] = "Not recorded"
        if member[field] not in VALID_PA_CPC_STATUSES:
            label = field.replace("_", " ").upper()
            raise ApiError(
                f"{label} must be Yes, No, Pending, or Not recorded.", 400
            )

    return member


def _validate_payment(data, allow_legacy_monthly_dues=False):
    if not isinstance(data, dict):
        raise ApiError("Payment details are required.", 400)

    payment_type = _validate_text(
        data.get("payment_type", "") or "",
        "Payment type",
        required=True,
        max_len=80,
    )
    if payment_type not in PAYMENT_TYPES and not (
        allow_legacy_monthly_dues and payment_type == "Monthly Dues"
    ):
        raise ApiError("Select a valid payment type.", 400)

    amount = _parse_float(
        data.get("amount"), "Payment amount must be a positive number."
    )
    if amount <= 0 or amount > 1_000_000_000:
        raise ApiError("Payment amount must be a positive number.", 400)

    payment_date = _validate_date(
        data.get("payment_date", "") or "", "Payment date", required=True
    )
    period_covered = _validate_text(
        data.get("period_covered", "") or "", "Period covered", max_len=7
    )
    if period_covered and not PERIOD_RE.match(period_covered):
        raise ApiError("Period covered must use YYYY-MM format.", 400)

    raw_balance = data.get("balance")
    if raw_balance in ("", None):
        balance = None
    else:
        balance = _parse_float(
            raw_balance, "Balance must be zero or a positive number."
        )
        if balance < 0 or balance > 1_000_000_000:
            raise ApiError("Balance must be zero or a positive number.", 400)

    return {
        "payment_type": payment_type,
        "payment_purpose": _validate_text(
            data.get("payment_purpose", "") or "", "Payment purpose", max_len=255
        ),
        "payment_date": payment_date,
        "period_covered": period_covered,
        "amount": amount,
        "receipt_number": _validate_text(
            data.get("receipt_number", "") or "", "Receipt number", max_len=100
        ),
        "voucher": _validate_text(
            data.get("voucher", "") or "", "Voucher", max_len=100
        ),
        "balance": balance,
        "payment_method": _validate_text(
            data.get("payment_method", "") or "", "Payment method", max_len=80
        ),
        "notes": _validate_text(data.get("notes", "") or "", "Notes", max_len=1000),
    }


def _validate_monthly_due(data):
    if not isinstance(data, dict):
        raise ApiError("Monthly dues details are required.", 400)
    period = _validate_text(
        data.get("period", "") or "", "Period", required=True, max_len=7
    )
    if not PERIOD_RE.match(period):
        raise ApiError("Period must use YYYY-MM format.", 400)
    raw_amount = data.get("amount", 0)
    if raw_amount in ("", None):
        raw_amount = 0
    amount = _parse_float(
        raw_amount, "Monthly dues amount must be zero or a positive number."
    )
    if amount < 0 or amount > 1_000_000_000:
        raise ApiError("Monthly dues amount must be zero or a positive number.", 400)
    return {
        "period": period,
        "status": _validate_text(
            data.get("status", "") or "", "Status", required=True, max_len=40
        ),
        "amount": amount,
        "notes": _validate_text(data.get("notes", "") or "", "Notes", max_len=1000),
    }


def _read_json_body():
    data = request.get_json(silent=True)
    if data is None:
        raise ApiError("Request body must be valid JSON.", 400)
    return data


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _create_token_session(user_id):
    OfficeSession = _models["OfficeSession"]
    token = secrets.token_urlsafe(32)
    sess = OfficeSession(
        token_hash=_token_hash(token),
        user_id=user_id,
        expires_at=_now_ms() + SESSION_LIFETIME_MS,
    )
    _db.session.add(sess)
    _db.session.commit()
    return token


def _get_token_user():
    auth_header = request.headers.get("Authorization", "")
    match = re.match(r"^Bearer ([A-Za-z0-9_-]+)$", auth_header)
    if not match:
        return None
    digest = _token_hash(match.group(1))
    OfficeSession = _models["OfficeSession"]
    OfficeUser = _models["OfficeUser"]
    sess = OfficeSession.query.filter(
        OfficeSession.token_hash == digest,
        OfficeSession.expires_at > _now_ms(),
    ).first()
    if not sess:
        return None
    user = _db.session.get(OfficeUser, sess.user_id)
    if not user:
        return None
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "token_hash": sess.token_hash,
        "auth_mode": "token",
    }


def _authenticated_user():
    token_user = _get_token_user()
    if token_user:
        return token_user
    if session.get("logged_in") and not session.get("office_db_signed_out"):
        return {
            "id": session.get("admin_user_id") or "website-admin",
            "username": session.get("admin_username") or "admin",
            "role": session.get("admin_role") or "Admin",
            "token_hash": None,
            "auth_mode": "website-admin",
        }
    return None


def _is_local_request():
    addr = request.remote_addr or ""
    return addr in ("127.0.0.1", "::1") or addr.startswith("::ffff:127.")


def _cell_str(val):
    if val is None:
        return ""
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, date):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val).strip()


def _parse_excel_amount(value):
    if value == "":
        return 0.0
    try:
        num = float(value.replace(",", ""))
        return num if math.isfinite(num) else float("nan")
    except ValueError:
        return float("nan")


def _parse_excel_date(value, row_num, label, errors, required=False):
    if not value:
        if required:
            errors.append(f"Payments row {row_num}: {label} is required.")
        return ""
    if not DATE_RE.match(value):
        errors.append(f"Row {row_num}: {label} must use YYYY-MM-DD format.")
        return ""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        errors.append(
            f"Row {row_num}: {label} must be a valid calendar date (YYYY-MM-DD)."
        )
        return ""


def _parse_excel_period(value, row_num, errors):
    if PERIOD_RE.match(value):
        return value
    if DATE_RE.match(value):
        try:
            dt = datetime.strptime(value, "%Y-%m-%d")
            return dt.strftime("%Y-%m")
        except ValueError:
            pass
    for fmt in ("%Y/%m/%d", "%Y/%m", "%b %Y", "%B %Y"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime("%Y-%m")
        except ValueError:
            continue
    errors.append(
        f"Monthly Dues row {row_num}: Period Covered must use YYYY-MM format."
    )
    return ""


def _read_sheet_rows(wb, sheet_name):
    if sheet_name not in wb.sheetnames:
        return []
    ws = wb[sheet_name]
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        return []

    headers = {}
    for idx, cell in enumerate(header_row):
        h = _cell_str(cell)
        if h:
            headers[idx] = h

    results = []
    for row_num, row_vals in enumerate(rows_iter, start=2):
        values = {}
        for idx, h in headers.items():
            raw = row_vals[idx] if idx < len(row_vals) else None
            values[h] = _cell_str(raw)
        if any(values.values()):
            results.append((row_num, values))
    return results


def _resolve_import_member_ref(values, row_num, sheet_name, errors):
    ref = {
        "member_code": values.get("Member Code", ""),
        "member_full_name": values.get("Member Full Name", ""),
        "plate_number": values.get("Plate Number", ""),
    }
    if not ref["member_code"] and not ref["member_full_name"] and not ref["plate_number"]:
        errors.append(
            f"{sheet_name} row {row_num}: Member Code, Member Full Name, or Plate Number is required."
        )
    return ref


def _parse_workbook(raw_bytes):
    wb = load_workbook(BytesIO(raw_bytes), data_only=True)
    errors = []
    seen_codes = set()

    members = []
    for row_num, values in _read_sheet_rows(wb, "Members"):
        member = {}
        for label, key in IMPORT_MEMBER_COLUMNS.items():
            if label in values:
                member[key] = values[label]

        member["full_name"] = (member.get("full_name") or "").strip()
        if not member["full_name"]:
            errors.append(f"Members row {row_num}: Full Name is required.")

        if member.get("member_code"):
            code_upper = member["member_code"].upper()
            if code_upper in seen_codes:
                errors.append(
                    f'Members row {row_num}: Member Code "{member["member_code"]}" occurs more than once in the workbook.'
                )
            seen_codes.add(code_upper)

        dues_amt = _parse_excel_amount(member.get("monthly_dues_amount") or "")
        if math.isnan(dues_amt) or dues_amt < 0:
            errors.append(
                f"Members row {row_num}: Monthly Dues Amount must be zero or a positive number."
            )
            dues_amt = 0.0
        member["monthly_dues_amount"] = dues_amt

        if member.get("due_day"):
            try:
                due_day_num = float(member["due_day"])
                if not due_day_num.is_integer() or int(due_day_num) < 1 or int(due_day_num) > 28:
                    raise ValueError
                member["due_day"] = int(due_day_num)
            except ValueError:
                errors.append(
                    f"Members row {row_num}: Due Day must be between 1 and 28."
                )
                member["due_day"] = None
        else:
            member["due_day"] = None

        if not member.get("status"):
            member["status"] = "Active"

        for field, label in (("pa_status", "PA Status"), ("cpc_status", "CPC Status")):
            val = member.get(field) or "Not recorded"
            member[field] = val
            if val not in VALID_PA_CPC_STATUSES:
                errors.append(
                    f"Members row {row_num}: {label} must be Yes, No, Pending, or Not recorded."
                )

        member["franchise_start_date"] = _parse_excel_date(
            member.get("franchise_start_date", ""),
            row_num,
            "Franchise Start Date",
            errors,
        )
        members.append({"rowNumber": row_num, "member": member})

    payments = []
    for row_num, values in _read_sheet_rows(wb, "Payments"):
        ref = _resolve_import_member_ref(values, row_num, "Payments", errors)
        payment_type = values.get("Payment Type", "")
        amount = _parse_excel_amount(values.get("Amount", ""))
        raw_balance = values.get("Balance", "")
        balance = _parse_excel_amount(raw_balance) if raw_balance else None

        payment = {
            **ref,
            "payment_date": _parse_excel_date(
                values.get("Payment Date", ""), row_num, "Payment Date", errors
            ),
            "payment_type": payment_type,
            "payment_purpose": values.get("Payment Purpose", ""),
            "period_covered": values.get("Period Covered (YYYY-MM)")
            or values.get("Period Covered", ""),
            "amount": amount,
            "receipt_number": values.get("Receipt Number", ""),
            "voucher": values.get("Voucher", ""),
            "balance": balance,
            "payment_method": values.get("Payment Method", ""),
            "batch_number": values.get("Batch Number", ""),
            "notes": values.get("Notes", ""),
        }
        if payment_type not in IMPORT_PAYMENT_TYPES:
            errors.append(
                f"Payments row {row_num}: Payment Type must be Membership Fee, Franchise Processing Fee, Monthly Dues, or Other Payment."
            )
        if math.isnan(amount) or amount <= 0:
            errors.append(
                f"Payments row {row_num}: Amount must be a positive number."
            )
            payment["amount"] = 0.0
        if balance is not None and (math.isnan(balance) or balance < 0):
            errors.append(
                f"Payments row {row_num}: Balance must be zero or a positive number."
            )
            payment["balance"] = None
        if (
            payment["payment_type"] == "Monthly Dues"
            and payment["period_covered"]
            and not PERIOD_RE.match(payment["period_covered"])
        ):
            errors.append(
                f"Payments row {row_num}: Period Covered must use YYYY-MM format."
            )
        payments.append({"rowNumber": row_num, "payment": payment})

    dues = []
    for row_num, values in _read_sheet_rows(wb, "Monthly Dues"):
        ref = _resolve_import_member_ref(values, row_num, "Monthly Dues", errors)
        period = _parse_excel_period(values.get("Period Covered", ""), row_num, errors)
        amount = _parse_excel_amount(values.get("Amount", ""))
        if math.isnan(amount) or amount < 0:
            errors.append(
                f"Monthly Dues row {row_num}: Amount must be zero or a positive number."
            )
            amount = 0.0
        status_val = (values.get("Status") or "").strip()
        if not status_val:
            errors.append(f"Monthly Dues row {row_num}: Status is required.")
        if len(status_val) > 40:
            errors.append(
                f"Monthly Dues row {row_num}: Status must be 40 characters or fewer."
            )
        dues.append(
            {
                "rowNumber": row_num,
                "due": {
                    **ref,
                    "batch_number": values.get("Batch Number", ""),
                    "period": period,
                    "status": status_val[:40],
                    "amount": amount,
                    "notes": values.get("Notes", ""),
                },
            }
        )

    documents = []
    for row_num, values in _read_sheet_rows(wb, "Documents"):
        ref = _resolve_import_member_ref(values, row_num, "Documents", errors)
        doc = {
            **ref,
            "document_type": values.get("Document Type", ""),
            "file_name": values.get("File Name", ""),
            "issue_date": _parse_excel_date(
                values.get("Issue Date", ""), row_num, "Issue Date", errors
            ),
            "expiry_date": _parse_excel_date(
                values.get("Expiry Date", ""), row_num, "Expiry Date", errors
            ),
            "notes": values.get("Notes", ""),
        }
        if not doc["document_type"]:
            errors.append(f"Documents row {row_num}: Document Type is required.")
        if not doc["file_name"]:
            errors.append(f"Documents row {row_num}: File Name is required.")
        documents.append({"rowNumber": row_num, "document": doc})

    return {
        "counts": {
            "members": len(members),
            "payments": len(payments),
            "dues": len(dues),
            "documents": len(documents),
        },
        "errors": errors,
        "members": members,
        "payments": payments,
        "dues": dues,
        "documents": documents,
    }


def _find_member_id_for_import(ref):
    OfficeMember = _models["OfficeMember"]
    if ref.get("member_code"):
        found = OfficeMember.query.filter(
            func.lower(OfficeMember.member_code) == ref["member_code"].lower()
        ).first()
        if not found:
            raise ApiError(
                f'Member Code "{ref["member_code"]}" was not found.', 400
            )
        return found.id

    if ref.get("plate_number"):
        matches = OfficeMember.query.filter(
            func.lower(OfficeMember.plate_number) == ref["plate_number"].strip().lower()
        ).all()
        if len(matches) == 1:
            return matches[0].id
        if len(matches) > 1:
            raise ApiError(
                f'Plate number "{ref["plate_number"]}" matches more than one member.',
                400,
            )
        raise ApiError(
            f'Plate number "{ref["plate_number"]}" was not found.', 400
        )

    if ref.get("member_full_name"):
        matches = OfficeMember.query.filter(
            func.lower(OfficeMember.full_name) == ref["member_full_name"].strip().lower()
        ).all()
        if len(matches) == 1:
            return matches[0].id
        if len(matches) > 1:
            raise ApiError(
                f'Member name "{ref["member_full_name"]}" matches more than one record. Add a unique Member Code to the workbook.',
                400,
            )

    ident = ref.get("member_code") or ref.get("plate_number") or ref.get("member_full_name")
    raise ApiError(
        f"No member matched {ident}. Import the member first or supply a matching Member Code.",
        400,
    )


def _apply_import(imported, workbook_sha256):
    OfficeMember = _models["OfficeMember"]
    OfficePayment = _models["OfficePayment"]
    OfficeMonthlyDue = _models["OfficeMonthlyDue"]
    OfficeDocument = _models["OfficeDocument"]
    OfficeImportBatch = _models["OfficeImportBatch"]

    timestamp = _now_iso()
    try:
        if _db.session.get(OfficeImportBatch, workbook_sha256):
            raise ApiError(
                "This workbook has already been imported. Importing it again could duplicate payments.",
                409,
            )

        default_pw_hash = generate_password_hash("Password123")
        for entry in imported["members"]:
            m_data = dict(entry["member"])
            if not m_data.get("member_code"):
                m_data["member_code"] = f"ENS-{uuid.uuid4().hex[:8].upper()}"

            existing = OfficeMember.query.filter(
                func.lower(OfficeMember.member_code) == m_data["member_code"].lower()
            ).first()
            if existing:
                for field in MEMBER_FIELDS:
                    if field in m_data:
                        setattr(existing, field, m_data[field])
                existing.updated_at = timestamp
                target_om = existing
            else:
                kwargs = {
                    "id": str(uuid.uuid4()),
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
                for field in MEMBER_FIELDS:
                    if field == "monthly_dues_amount":
                        kwargs[field] = m_data.get(field, 0.0)
                    elif field == "due_day":
                        kwargs[field] = m_data.get(field, None)
                    elif field == "status":
                        kwargs[field] = m_data.get(field) or "Active"
                    elif field in ("pa_status", "cpc_status"):
                        kwargs[field] = m_data.get(field) or "Not recorded"
                    else:
                        kwargs[field] = m_data.get(field, "") or ""
                target_om = OfficeMember(**kwargs)
                _db.session.add(target_om)
            _sync_office_member_to_portal(target_om, default_pw_hash=default_pw_hash)
            _db.session.flush()

        for entry in imported["payments"]:
            p = entry["payment"]
            member_id = _find_member_id_for_import(p)
            _db.session.add(
                OfficePayment(
                    id=str(uuid.uuid4()),
                    member_id=member_id,
                    payment_date=p["payment_date"],
                    payment_type=p["payment_type"],
                    payment_purpose=p["payment_purpose"],
                    period_covered=p["period_covered"],
                    amount=p["amount"],
                    receipt_number=p["receipt_number"],
                    voucher=p["voucher"],
                    balance=p["balance"],
                    payment_method=p["payment_method"],
                    notes=p["notes"],
                    created_at=timestamp,
                )
            )

        for entry in imported["dues"]:
            d = entry["due"]
            member_id = _find_member_id_for_import(d)
            existing_due = OfficeMonthlyDue.query.filter_by(
                member_id=member_id, period=d["period"]
            ).first()
            if existing_due:
                existing_due.status = d["status"]
                existing_due.amount = d["amount"]
                existing_due.notes = d["notes"]
                existing_due.updated_at = timestamp
            else:
                _db.session.add(
                    OfficeMonthlyDue(
                        id=str(uuid.uuid4()),
                        member_id=member_id,
                        period=d["period"],
                        status=d["status"],
                        amount=d["amount"],
                        notes=d["notes"],
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                )

        for entry in imported["documents"]:
            doc = entry["document"]
            member_id = _find_member_id_for_import(doc)
            _db.session.add(
                OfficeDocument(
                    id=str(uuid.uuid4()),
                    member_id=member_id,
                    document_type=doc["document_type"],
                    original_filename=doc["file_name"],
                    stored_filename=None,
                    file_data=None,
                    mime_type="",
                    size_bytes=0,
                    issue_date=doc["issue_date"],
                    expiry_date=doc["expiry_date"],
                    created_at=timestamp,
                )
            )

        _db.session.add(
            OfficeImportBatch(
                workbook_sha256=workbook_sha256,
                imported_at=timestamp,
                member_count=len(imported["members"]),
                payment_count=len(imported["payments"]),
                dues_count=len(imported["dues"]),
            )
        )
        _db.session.commit()
    except Exception:
        _db.session.rollback()
        raise

    return {
        "members": len(imported["members"]),
        "payments": len(imported["payments"]),
        "monthlyDues": len(imported["dues"]),
        "documentsListed": len(imported["documents"]),
    }


def _build_export_workbook():
    OfficeMember = _models["OfficeMember"]
    OfficePayment = _models["OfficePayment"]
    OfficeMonthlyDue = _models["OfficeMonthlyDue"]
    OfficeDocument = _models["OfficeDocument"]

    wb = Workbook()
    # Remove default sheet
    wb.remove(wb.active)
    wb.properties.creator = "Enasia Transport"
    wb.properties.title = "Enasia Member Database Export"
    wb.properties.subject = (
        "Enasia Transport member, payment, dues, and document records"
    )

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(
        start_color="174A63", end_color="174A63", fill_type="solid"
    )
    header_alignment = Alignment(vertical="center", wrap_text=True)

    def setup_sheet(title, headers, widths):
        ws = wb.create_sheet(title=title)
        ws.freeze_panes = "A2"
        ws.append(headers)
        ws.row_dimensions[1].height = 32
        for idx, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = width
            cell = ws.cell(row=1, column=idx)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"
        return ws

    # 1. Members sheet
    member_sheet = setup_sheet(
        "Members",
        [
            "Member Code",
            "Full Name",
            "Address",
            "ID Number",
            "Driver License Number",
            "Unit Name",
            "Vehicle Make",
            "Vehicle Model",
            "Vehicle Year",
            "Plate Number",
            "MV File Number",
            "Franchise Application Case Number",
            "Batch Number",
            "Franchise Start Date",
            "Monthly Dues Amount",
            "Due Day (1-28)",
            "Member Status",
            "MOA Status",
            "Lease Status",
            "PA Status",
            "CPC Status",
            "Original OR/CR",
            "Facebook Name",
            "Terms",
            "Notes",
        ],
        [
            18, 30, 42, 20, 25, 25, 20, 20, 16, 20, 22, 34, 18, 22, 22, 16, 18,
            20, 20, 18, 18, 22, 22, 20, 38,
        ],
    )
    members = OfficeMember.query.order_by(func.lower(OfficeMember.full_name)).all()
    members_by_id = {m.id: m for m in members}
    for m in members:
        member_sheet.append(
            [
                "" if getattr(m, col) is None else getattr(m, col)
                for col in MEMBER_FIELDS
            ]
        )

    # 2. Payments sheet
    payment_sheet = setup_sheet(
        "Payments",
        [
            "Member Code",
            "Member Full Name",
            "Plate Number",
            "Payment Date",
            "Payment Type",
            "Payment Purpose",
            "Period Covered (YYYY-MM)",
            "Amount",
            "Receipt Number",
            "Voucher",
            "Balance",
            "Payment Method",
            "Batch Number",
            "Notes",
        ],
        [18, 30, 18, 18, 18, 32, 28, 28, 16, 20, 18, 20, 18, 40],
    )
    payments = OfficePayment.query.order_by(
        OfficePayment.payment_date, OfficePayment.created_at
    ).all()
    for p in payments:
        m = members_by_id.get(p.member_id)
        if not m:
            continue
        payment_sheet.append(
            [
                m.member_code or "",
                m.full_name or "",
                m.plate_number or "",
                p.payment_date or "",
                p.payment_type or "",
                p.payment_purpose or "",
                p.period_covered or "",
                p.amount,
                p.receipt_number or "",
                p.voucher or "",
                p.balance,
                p.payment_method or "",
                m.batch_number or "",
                p.notes or "",
            ]
        )

    # 3. Monthly Dues sheet
    dues_sheet = setup_sheet(
        "Monthly Dues",
        [
            "Member Code",
            "Member Full Name",
            "Plate Number",
            "Batch Number",
            "Period Covered",
            "Status",
            "Amount",
            "Notes",
        ],
        [18, 30, 18, 18, 28, 20, 16, 38],
    )
    dues_rows = (
        _db.session.query(OfficeMonthlyDue, OfficeMember)
        .join(OfficeMember, OfficeMember.id == OfficeMonthlyDue.member_id)
        .order_by(OfficeMonthlyDue.period, func.lower(OfficeMember.full_name))
        .all()
    )
    for d, m in dues_rows:
        dues_sheet.append(
            [
                m.member_code or "",
                m.full_name or "",
                m.plate_number or "",
                m.batch_number or "",
                d.period or "",
                d.status or "",
                d.amount,
                d.notes or "",
            ]
        )

    # 4. Documents sheet
    doc_sheet = setup_sheet(
        "Documents",
        [
            "Member Code",
            "Member Full Name",
            "Plate Number",
            "Document Type",
            "File Name",
            "Issue Date",
            "Expiry Date",
            "Notes",
        ],
        [18, 30, 18, 30, 40, 18, 18, 36],
    )
    doc_rows = (
        _db.session.query(OfficeDocument, OfficeMember)
        .join(OfficeMember, OfficeMember.id == OfficeDocument.member_id)
        .order_by(func.lower(OfficeMember.full_name), OfficeDocument.created_at)
        .all()
    )
    for doc, m in doc_rows:
        has_file = bool(doc.file_data or doc.stored_filename)
        upload_status = (
            f"Uploaded ({doc.mime_type}, {doc.size_bytes} bytes); stored securely in database."
            if has_file
            else "Listed only; no file uploaded."
        )
        doc_sheet.append(
            [
                m.member_code or "",
                m.full_name or "",
                m.plate_number or "",
                doc.document_type or "",
                doc.original_filename or "",
                doc.issue_date or "",
                doc.expiry_date or "",
                upload_status,
            ]
        )

    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return out.getvalue()


def _serialize_member_summary(m):
    return {
        "id": m.id,
        "member_code": m.member_code,
        "full_name": m.full_name,
        "address": m.address,
        "unit_name": m.unit_name,
        "vehicle_make": m.vehicle_make,
        "vehicle_model": m.vehicle_model,
        "plate_number": m.plate_number,
        "mv_file_number": m.mv_file_number,
        "franchise_case_number": m.franchise_case_number,
        "batch_number": m.batch_number,
        "monthly_dues_amount": m.monthly_dues_amount,
        "status": m.status,
    }


def _serialize_member_full(m):
    data = {"id": m.id, "created_at": m.created_at, "updated_at": m.updated_at}
    for field in MEMBER_FIELDS:
        data[field] = getattr(m, field)
    return data


def _serialize_payment(p):
    return {
        "id": p.id,
        "member_id": p.member_id,
        "payment_date": p.payment_date,
        "payment_type": p.payment_type,
        "payment_purpose": p.payment_purpose,
        "period_covered": p.period_covered,
        "amount": p.amount,
        "receipt_number": p.receipt_number,
        "voucher": p.voucher,
        "balance": p.balance,
        "payment_method": p.payment_method,
        "notes": p.notes,
        "created_at": p.created_at,
    }


def _serialize_due(d):
    return {
        "id": d.id,
        "member_id": d.member_id,
        "period": d.period,
        "status": d.status,
        "amount": d.amount,
        "notes": d.notes,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
    }


def _serialize_document(doc):
    return {
        "id": doc.id,
        "document_type": doc.document_type,
        "original_filename": doc.original_filename,
        "mime_type": doc.mime_type,
        "size_bytes": doc.size_bytes,
        "uploaded": bool(doc.file_data or doc.stored_filename),
        "issue_date": doc.issue_date,
        "expiry_date": doc.expiry_date,
        "created_at": doc.created_at,
    }


def _require_auth(require_write=False, require_admin=False):
    user = _authenticated_user()
    if not user:
        raise ApiError("Sign in to continue.", 401)
    if require_write and user["role"] == "View only":
        raise ApiError("View-only accounts cannot change database records.", 403)
    if require_admin and user["role"] != "Admin":
        raise ApiError("Only an administrator can manage user accounts.", 403)
    return user


@office_db_bp.errorhandler(ApiError)
def _handle_api_error(err):
    payload = {"error": err.message}
    payload.update(err.extra)
    return _json_response(payload, err.status)


@office_db_bp.route("")
@office_db_bp.route("/")
def index():
    if request.args.get("auto") == "1" and session.get("logged_in"):
        session.pop("office_db_signed_out", None)
    resp = current_app.make_response(render_template("office_database.html"))
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@office_db_bp.route("/api/status", methods=["GET"])
def api_status():
    OfficeUser = _models["OfficeUser"]
    user_count = OfficeUser.query.count()
    website_admin_logged_in = bool(session.get("logged_in"))
    user = _authenticated_user()
    return _json_response(
        {
            "setupRequired": (user_count == 0 and not website_admin_logged_in),
            "application": "Enasia Member Database",
            "hosting": False,
            "websiteAdminLoggedIn": website_admin_logged_in,
            "autoSignedIn": bool(user),
            "username": user["username"] if user else "",
            "role": user["role"] if user else "",
            "authMode": user["auth_mode"] if user else "",
            "hasStaffAccounts": user_count > 0,
        }
    )


@office_db_bp.route("/api/admin-resume", methods=["POST"])
def api_admin_resume():
    if not session.get("logged_in"):
        raise ApiError("Please sign in to the EnAsia Admin Dashboard first.", 401)
    session.pop("office_db_signed_out", None)
    return _json_response(
        {
            "username": session.get("admin_username") or "admin",
            "role": session.get("admin_role") or "Admin",
            "authMode": "website-admin",
        }
    )


@office_db_bp.route("/api/setup", methods=["POST"])
def api_setup():
    OfficeUser = _models["OfficeUser"]
    if OfficeUser.query.count() != 0:
        raise ApiError("The administrator account has already been created.", 409)
    if not session.get("logged_in") and not _is_local_request():
        raise ApiError(
            "Sign in to the EnAsia Admin Dashboard first to create staff accounts.",
            403,
        )
    body = _read_json_body()
    username = _validate_text(body.get("username", ""), "Username", required=True, max_len=50)
    password = _validate_text(body.get("password", ""), "Password", required=True, max_len=200)
    if not USERNAME_RE.match(username):
        raise ApiError(
            "Username must be 3-50 letters, numbers, dots, underscores, or hyphens.",
            400,
        )
    if len(password) < 6:
        raise ApiError("Password must be at least 6 characters.", 400)

    user_id = str(uuid.uuid4())
    user = OfficeUser(
        id=user_id,
        username=username,
        password_hash=generate_password_hash(password),
        role="Admin",
        created_at=_now_iso(),
    )
    _db.session.add(user)
    _db.session.commit()
    token = _create_token_session(user_id)
    session.pop("office_db_signed_out", None)
    return _json_response({"token": token, "username": username, "role": "Admin"}, 201)


@office_db_bp.route("/api/login", methods=["POST"])
def api_login():
    OfficeUser = _models["OfficeUser"]
    remote_addr = request.remote_addr or "unknown"
    now_ms = _now_ms()
    attempts = _login_attempts.get(remote_addr, {"count": 0, "last": 0})
    if attempts["count"] >= 10 and now_ms - attempts["last"] < 15 * 60 * 1000:
        raise ApiError("Too many sign-in attempts. Try again in 15 minutes.", 429)

    body = _read_json_body()
    username = _validate_text(body.get("username", ""), "Username", required=True, max_len=50)
    password = _validate_text(body.get("password", ""), "Password", required=True, max_len=200)

    user = OfficeUser.query.filter(
        func.lower(OfficeUser.username) == username.lower()
    ).first()
    if not user or not check_password_hash(user.password_hash, password):
        new_count = (
            attempts["count"] + 1
            if now_ms - attempts["last"] < 15 * 60 * 1000
            else 1
        )
        _login_attempts[remote_addr] = {"count": new_count, "last": now_ms}
        raise ApiError("Username or password is incorrect.", 401)

    _login_attempts.pop(remote_addr, None)
    token = _create_token_session(user.id)
    return _json_response(
        {"token": token, "username": user.username, "role": user.role}, 200
    )


@office_db_bp.route("/api/logout", methods=["POST"])
def api_logout():
    OfficeSession = _models["OfficeSession"]
    token_user = _get_token_user()
    if token_user and token_user.get("token_hash"):
        sess = _db.session.get(OfficeSession, token_user["token_hash"])
        if sess:
            _db.session.delete(sess)
            _db.session.commit()
    if session.get("logged_in"):
        session["office_db_signed_out"] = True
    return _json_response({"success": True})


@office_db_bp.route("/api/users", methods=["GET", "POST"])
def api_users():
    OfficeUser = _models["OfficeUser"]
    _require_auth(require_write=(request.method != "GET"), require_admin=True)

    if request.method == "GET":
        users = OfficeUser.query.order_by(func.lower(OfficeUser.username)).all()
        return _json_response(
            {
                "users": [
                    {
                        "id": u.id,
                        "username": u.username,
                        "role": u.role,
                        "created_at": u.created_at,
                    }
                    for u in users
                ]
            }
        )

    body = _read_json_body()
    username = _validate_text(body.get("username", ""), "Username", required=True, max_len=50)
    password = _validate_text(body.get("password", ""), "Password", required=True, max_len=200)
    role = _validate_text(body.get("role", ""), "Role", required=True, max_len=30)
    if not USERNAME_RE.match(username):
        raise ApiError(
            "Username must be 3-50 letters, numbers, dots, underscores, or hyphens.",
            400,
        )
    if len(password) < 6:
        raise ApiError("Password must be at least 6 characters.", 400)
    if role not in USER_ROLES:
        raise ApiError("Choose Admin, Editor, or View only.", 400)
    if OfficeUser.query.filter(
        func.lower(OfficeUser.username) == username.lower()
    ).first():
        raise ApiError("That username is already in use.", 409)

    user_id = str(uuid.uuid4())
    user = OfficeUser(
        id=user_id,
        username=username,
        password_hash=generate_password_hash(password),
        role=role,
        created_at=_now_iso(),
    )
    _db.session.add(user)
    _db.session.commit()
    return _json_response({"id": user_id, "username": username, "role": role}, 201)


@office_db_bp.route("/api/users/<user_id>", methods=["PUT", "DELETE"])
def api_user_item(user_id):
    OfficeUser = _models["OfficeUser"]
    _require_auth(require_write=True, require_admin=True)
    if not UUID_RE.match(user_id):
        raise ApiError("User account not found.", 404)

    target_user = _db.session.get(OfficeUser, user_id)
    if not target_user:
        raise ApiError("User account not found.", 404)

    if request.method == "DELETE":
        if target_user.role == "Admin":
            admin_count = OfficeUser.query.filter_by(role="Admin").count()
            if admin_count <= 1:
                raise ApiError("Cannot delete the last remaining Admin account.", 400)
        _models["OfficeSession"].query.filter_by(user_id=target_user.id).delete()
        _db.session.delete(target_user)
        _db.session.commit()
        return _json_response({"success": True})

    body = _read_json_body()
    new_role = body.get("role")
    new_password = body.get("password")

    if new_role is not None:
        role_clean = _validate_text(new_role, "Role", required=True, max_len=30)
        if role_clean not in USER_ROLES:
            raise ApiError("Choose Admin, Editor, or View only.", 400)
        if target_user.role == "Admin" and role_clean != "Admin":
            admin_count = OfficeUser.query.filter_by(role="Admin").count()
            if admin_count <= 1:
                raise ApiError("At least one Admin account must remain.", 400)
        target_user.role = role_clean

    if new_password is not None and str(new_password).strip() != "":
        pw_clean = _validate_text(new_password, "Password", required=True, max_len=200)
        if len(pw_clean) < 6:
            raise ApiError("Password must be at least 6 characters.", 400)
        target_user.password_hash = generate_password_hash(pw_clean)

    _db.session.commit()
    return _json_response(
        {"id": target_user.id, "username": target_user.username, "role": target_user.role}
    )


@office_db_bp.route("/api/export", methods=["GET"])
def api_export():
    _require_auth(require_write=False)
    xlsx_bytes = _build_export_workbook()
    return Response(
        xlsx_bytes,
        status=200,
        headers={
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "Content-Length": str(len(xlsx_bytes)),
            "Content-Disposition": "attachment; filename*=UTF-8''Enasia-Member-Database.xlsx",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@office_db_bp.route("/api/members", methods=["GET", "POST"])
def api_members():
    OfficeMember = _models["OfficeMember"]
    _require_auth(require_write=(request.method != "GET"))

    if request.method == "GET":
        search = (request.args.get("search") or "").strip()
        q = OfficeMember.query
        if search:
            like = f"%{search}%"
            q = q.filter(
                or_(
                    OfficeMember.full_name.ilike(like),
                    OfficeMember.member_code.ilike(like),
                    OfficeMember.plate_number.ilike(like),
                    OfficeMember.mv_file_number.ilike(like),
                    OfficeMember.franchise_case_number.ilike(like),
                    OfficeMember.batch_number.ilike(like),
                    OfficeMember.id_number.ilike(like),
                    OfficeMember.driver_license_number.ilike(like),
                )
            )
        members = (
            q.order_by(func.lower(OfficeMember.full_name)).limit(500).all()
        )
        return _json_response(
            {"members": [_serialize_member_summary(m) for m in members]}
        )

    member_data = _validate_member(_read_json_body())
    if not member_data["member_code"]:
        member_data["member_code"] = f"ENS-{uuid.uuid4().hex[:8].upper()}"
    if OfficeMember.query.filter(
        func.lower(OfficeMember.member_code) == member_data["member_code"].lower()
    ).first():
        raise ApiError("That member code is already in use.", 409)

    member_id = str(uuid.uuid4())
    ts = _now_iso()
    new_member = OfficeMember(
        id=member_id,
        created_at=ts,
        updated_at=ts,
        **member_data,
    )
    _db.session.add(new_member)
    _sync_office_member_to_portal(new_member)
    _db.session.commit()
    return _json_response({"id": member_id}, 201)


@office_db_bp.route("/api/members/<member_id>", methods=["GET", "PUT"])
def api_member_detail(member_id):
    OfficeMember = _models["OfficeMember"]
    OfficePayment = _models["OfficePayment"]
    OfficeMonthlyDue = _models["OfficeMonthlyDue"]
    OfficeDocument = _models["OfficeDocument"]

    _require_auth(require_write=(request.method != "GET"))
    if not UUID_RE.match(member_id):
        raise ApiError("Member not found.", 404)

    member = _db.session.get(OfficeMember, member_id)
    if not member:
        raise ApiError("Member not found.", 404)

    if request.method == "GET":
        payments = (
            OfficePayment.query.filter_by(member_id=member.id)
            .order_by(OfficePayment.payment_date.desc(), OfficePayment.created_at.desc())
            .all()
        )
        monthly_dues = (
            OfficeMonthlyDue.query.filter_by(member_id=member.id)
            .order_by(OfficeMonthlyDue.period.desc())
            .all()
        )
        documents = (
            OfficeDocument.query.filter_by(member_id=member.id)
            .order_by(OfficeDocument.created_at.desc())
            .all()
        )
        totals_rows = (
            _db.session.query(
                OfficePayment.payment_type, func.sum(OfficePayment.amount)
            )
            .filter(OfficePayment.member_id == member.id)
            .group_by(OfficePayment.payment_type)
            .all()
        )
        totals = [
            {"payment_type": p_type, "total": float(total or 0.0)}
            for p_type, total in totals_rows
        ]
        return _json_response(
            {
                "member": _serialize_member_full(member),
                "payments": [_serialize_payment(p) for p in payments],
                "monthlyDues": [_serialize_due(d) for d in monthly_dues],
                "documents": [_serialize_document(doc) for doc in documents],
                "totals": totals,
            }
        )

    member_data = _validate_member(_read_json_body())
    if not member_data["member_code"]:
        member_data["member_code"] = f"ENS-{uuid.uuid4().hex[:8].upper()}"
    conflict = OfficeMember.query.filter(
        func.lower(OfficeMember.member_code) == member_data["member_code"].lower(),
        OfficeMember.id != member.id,
    ).first()
    if conflict:
        raise ApiError("That member code is already in use.", 409)

    old_code = member.member_code
    for field in MEMBER_FIELDS:
        setattr(member, field, member_data[field])
    member.updated_at = _now_iso()
    _sync_office_member_to_portal(member, old_code=old_code)
    _db.session.commit()
    return _json_response({"success": True})


@office_db_bp.route("/api/payments", methods=["POST"])
def api_payments_create():
    OfficeMember = _models["OfficeMember"]
    OfficePayment = _models["OfficePayment"]
    _require_auth(require_write=True)

    body = _read_json_body()
    member_id = _validate_text(
        body.get("member_id", ""), "Member", required=True, max_len=64
    )
    if not _db.session.get(OfficeMember, member_id):
        raise ApiError("Member not found.", 404)

    payment_data = _validate_payment(body)
    payment_id = str(uuid.uuid4())
    _db.session.add(
        OfficePayment(
            id=payment_id,
            member_id=member_id,
            created_at=_now_iso(),
            **payment_data,
        )
    )
    _db.session.commit()
    return _json_response({"id": payment_id}, 201)


@office_db_bp.route("/api/payments/<payment_id>", methods=["PUT", "DELETE"])
def api_payment_item(payment_id):
    OfficePayment = _models["OfficePayment"]
    _require_auth(require_write=True)
    if not UUID_RE.match(payment_id):
        raise ApiError("Payment not found.", 404)

    existing = _db.session.get(OfficePayment, payment_id)
    if not existing:
        raise ApiError("Payment not found.", 404)

    if request.method == "DELETE":
        _db.session.delete(existing)
        _db.session.commit()
        return _json_response({"success": True})

    payment_data = _validate_payment(
        _read_json_body(),
        allow_legacy_monthly_dues=(existing.payment_type == "Monthly Dues"),
    )
    for k, v in payment_data.items():
        setattr(existing, k, v)
    _db.session.commit()
    return _json_response({"success": True})


@office_db_bp.route("/api/monthly-dues", methods=["POST"])
def api_monthly_dues_create():
    OfficeMember = _models["OfficeMember"]
    OfficeMonthlyDue = _models["OfficeMonthlyDue"]
    _require_auth(require_write=True)

    body = _read_json_body()
    member_id = _validate_text(
        body.get("member_id", ""), "Member", required=True, max_len=64
    )
    if not _db.session.get(OfficeMember, member_id):
        raise ApiError("Member not found.", 404)

    due_data = _validate_monthly_due(body)
    ts = _now_iso()
    existing = OfficeMonthlyDue.query.filter_by(
        member_id=member_id, period=due_data["period"]
    ).first()
    if existing:
        existing.status = due_data["status"]
        existing.amount = due_data["amount"]
        existing.notes = due_data["notes"]
        existing.updated_at = ts
    else:
        _db.session.add(
            OfficeMonthlyDue(
                id=str(uuid.uuid4()),
                member_id=member_id,
                period=due_data["period"],
                status=due_data["status"],
                amount=due_data["amount"],
                notes=due_data["notes"],
                created_at=ts,
                updated_at=ts,
            )
        )
    _db.session.commit()
    return _json_response({"success": True}, 201)


@office_db_bp.route("/api/monthly-dues/<due_id>", methods=["PUT", "DELETE"])
def api_monthly_dues_item(due_id):
    OfficeMonthlyDue = _models["OfficeMonthlyDue"]
    _require_auth(require_write=True)
    if not UUID_RE.match(due_id):
        raise ApiError("Monthly dues status not found.", 404)

    existing = _db.session.get(OfficeMonthlyDue, due_id)
    if not existing:
        raise ApiError("Monthly dues status not found.", 404)

    if request.method == "DELETE":
        _db.session.delete(existing)
        _db.session.commit()
        return _json_response({"success": True})

    due_data = _validate_monthly_due(_read_json_body())
    conflict = OfficeMonthlyDue.query.filter(
        OfficeMonthlyDue.member_id == existing.member_id,
        OfficeMonthlyDue.period == due_data["period"],
        OfficeMonthlyDue.id != existing.id,
    ).first()
    if conflict:
        raise ApiError("A monthly dues status already exists for that period.", 409)

    existing.period = due_data["period"]
    existing.status = due_data["status"]
    existing.amount = due_data["amount"]
    existing.notes = due_data["notes"]
    existing.updated_at = _now_iso()
    _db.session.commit()
    return _json_response({"success": True})


@office_db_bp.route("/api/members/<member_id>/documents", methods=["POST"])
def api_member_documents_upload(member_id):
    OfficeMember = _models["OfficeMember"]
    OfficeDocument = _models["OfficeDocument"]
    _require_auth(require_write=True)
    if not UUID_RE.match(member_id) or not _db.session.get(OfficeMember, member_id):
        raise ApiError("Member not found.", 404)

    body = _read_json_body()
    doc_id_in = _validate_text(body.get("document_id") or "", "Document ID", max_len=64)
    existing_doc = None
    if doc_id_in:
        existing_doc = OfficeDocument.query.filter_by(
            id=doc_id_in, member_id=member_id
        ).first()
        if not existing_doc:
            raise ApiError("Document not found for this member.", 404)

    document_type = _validate_text(
        body.get("document_type")
        or (existing_doc.document_type if existing_doc else ""),
        "Document type",
        required=True,
        max_len=100,
    )
    issue_date = _validate_date(
        body.get("issue_date")
        if body.get("issue_date") is not None
        else (existing_doc.issue_date if existing_doc else ""),
        "Issue date",
    )
    expiry_date = _validate_date(
        body.get("expiry_date")
        if body.get("expiry_date") is not None
        else (existing_doc.expiry_date if existing_doc else ""),
        "Expiry date",
    )
    raw_filename = _validate_text(
        body.get("filename", ""), "File name", required=True, max_len=255
    )
    filename = re.sub(r"[\x00-\x1f]", "_", os.path.basename(raw_filename))
    mime_type = _validate_text(
        body.get("mime_type", ""), "File type", required=True, max_len=100
    )
    ext = os.path.splitext(filename)[1].lower()
    if ext not in {".pdf", ".jpg", ".jpeg", ".png"}:
        raise ApiError("Upload PDF, JPG, or PNG documents only.", 400)
    if mime_type not in {"application/pdf", "image/jpeg", "image/png"}:
        raise ApiError(
            "File content type does not match an allowed PDF, JPG, or PNG document.",
            400,
        )

    b64_content = body.get("content_base64")
    if not isinstance(b64_content, str) or not BASE64_RE.match(b64_content):
        raise ApiError("Document file content is invalid.", 400)
    try:
        content = base64.b64decode(b64_content, validate=True)
    except Exception:
        raise ApiError("Document file content is invalid.", 400)

    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise ApiError("Documents must be between 1 byte and 20 MB.", 400)

    sig_ok = (
        (ext == ".pdf" and content[:5] == b"%PDF-")
        or (ext in (".jpg", ".jpeg") and content[:3] == b"\xff\xd8\xff")
        or (ext == ".png" and content[:8] == b"\x89PNG\r\n\x1a\n")
    )
    if not sig_ok:
        raise ApiError("The uploaded file content does not match its file type.", 400)

    new_uuid = str(uuid.uuid4())
    stored_filename = f"{new_uuid}{ext}"
    if existing_doc:
        existing_doc.document_type = document_type
        existing_doc.original_filename = filename
        existing_doc.stored_filename = stored_filename
        existing_doc.file_data = content
        existing_doc.mime_type = mime_type
        existing_doc.size_bytes = len(content)
        existing_doc.issue_date = issue_date
        existing_doc.expiry_date = expiry_date
        _db.session.commit()
        return _json_response({"id": existing_doc.id}, 200)

    doc = OfficeDocument(
        id=new_uuid,
        member_id=member_id,
        document_type=document_type,
        original_filename=filename,
        stored_filename=stored_filename,
        file_data=content,
        mime_type=mime_type,
        size_bytes=len(content),
        issue_date=issue_date,
        expiry_date=expiry_date,
        created_at=_now_iso(),
    )
    _db.session.add(doc)
    _db.session.commit()
    return _json_response({"id": new_uuid}, 201)


@office_db_bp.route("/api/documents/<document_id>", methods=["GET", "PUT", "DELETE"])
def api_document_item(document_id):
    OfficeDocument = _models["OfficeDocument"]
    _require_auth(require_write=(request.method != "GET"))
    if not UUID_RE.match(document_id):
        raise ApiError("Document not found.", 404)

    doc = _db.session.get(OfficeDocument, document_id)
    if not doc:
        raise ApiError("Document not found.", 404)

    if request.method == "PUT":
        body = _read_json_body()
        doc.document_type = _validate_text(
            body.get("document_type", "") or "",
            "Document type",
            required=True,
            max_len=100,
        )
        doc.issue_date = _validate_date(body.get("issue_date", "") or "", "Issue date")
        doc.expiry_date = _validate_date(
            body.get("expiry_date", "") or "", "Expiry date"
        )
        _db.session.commit()
        return _json_response({"success": True})

    if request.method == "DELETE":
        _db.session.delete(doc)
        _db.session.commit()
        return _json_response({"success": True})

    if not doc.file_data:
        raise ApiError(
            "This document is listed in the import workbook but has not been uploaded yet.",
            404,
        )
    encoded_name = quote(doc.original_filename or "member-document")
    return Response(
        doc.file_data,
        status=200,
        headers={
            "Content-Type": doc.mime_type or "application/octet-stream",
            "Content-Length": str(len(doc.file_data)),
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@office_db_bp.route("/api/import/preview", methods=["POST"])
@office_db_bp.route("/api/import/commit", methods=["POST"])
def api_import():
    OfficeImportBatch = _models["OfficeImportBatch"]
    _require_auth(require_write=True)

    body = _read_json_body()
    b64 = body.get("workbook_base64")
    if not isinstance(b64, str) or not BASE64_RE.match(b64):
        raise ApiError("Select a valid Excel workbook.", 400)
    try:
        raw_bytes = base64.b64decode(b64, validate=True)
    except Exception:
        raise ApiError("Select a valid Excel workbook.", 400)

    if not raw_bytes or len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise ApiError("Excel workbooks must be between 1 byte and 20 MB.", 400)

    sha256_hex = hashlib.sha256(raw_bytes).hexdigest()
    try:
        imported = _parse_workbook(raw_bytes)
    except Exception:
        raise ApiError(
            "The selected file could not be read as an Excel workbook.", 400
        )

    if not imported["counts"]["members"]:
        imported["errors"].insert(
            0, 'The workbook has no member rows. Add rows to the "Members" sheet.'
        )
    if imported["errors"]:
        return _json_response(
            {"counts": imported["counts"], "errors": imported["errors"]}, 400
        )

    already_imported = bool(_db.session.get(OfficeImportBatch, sha256_hex))
    if request.path.endswith("/commit"):
        if already_imported:
            raise ApiError(
                "This exact workbook has already been imported. Importing it again could duplicate payments.",
                409,
            )
        summary = _apply_import(imported, sha256_hex)
        return _json_response({"imported": summary}, 200)

    return _json_response(
        {
            "counts": imported["counts"],
            "ready": True,
            "alreadyImported": already_imported,
        },
        200,
    )


def _seed_office_db_if_empty(app):
    OfficeMember = _models["OfficeMember"]
    OfficePayment = _models["OfficePayment"]
    OfficeMonthlyDue = _models["OfficeMonthlyDue"]
    OfficeDocument = _models["OfficeDocument"]
    OfficeImportBatch = _models["OfficeImportBatch"]

    if OfficeMember.query.first() is not None:
        return

    seed_path = os.path.join(app.root_path, "seed_office_db.json")
    if not os.path.isfile(seed_path):
        return

    with open(seed_path, "r", encoding="utf-8") as fh:
        seed = json.load(fh)

    for m in seed.get("members", []):
        _db.session.add(
            OfficeMember(
                id=m["id"],
                member_code=m.get("member_code") or f"ENS-{uuid.uuid4().hex[:8].upper()}",
                full_name=m["full_name"],
                address=m.get("address") or "",
                id_number=m.get("id_number") or "",
                driver_license_number=m.get("driver_license_number") or "",
                unit_name=m.get("unit_name") or "",
                vehicle_make=m.get("vehicle_make") or "",
                vehicle_model=m.get("vehicle_model") or "",
                vehicle_year=m.get("vehicle_year") or "",
                plate_number=m.get("plate_number") or "",
                mv_file_number=m.get("mv_file_number") or "",
                franchise_case_number=m.get("franchise_case_number") or "",
                batch_number=m.get("batch_number") or "",
                franchise_start_date=m.get("franchise_start_date") or "",
                monthly_dues_amount=float(m.get("monthly_dues_amount") or 0.0),
                due_day=m.get("due_day"),
                status=m.get("status") or "Active",
                moa_status=m.get("moa_status") or "",
                lease_status=m.get("lease_status") or "",
                pa_status=m.get("pa_status") or "Not recorded",
                cpc_status=m.get("cpc_status") or "Not recorded",
                original_or_cr=m.get("original_or_cr") or "",
                facebook_name=m.get("facebook_name") or "",
                terms=m.get("terms") or "",
                notes=m.get("notes") or "",
                created_at=m.get("created_at") or _now_iso(),
                updated_at=m.get("updated_at") or _now_iso(),
            )
        )
    _db.session.flush()

    for p in seed.get("payments", []):
        _db.session.add(
            OfficePayment(
                id=p["id"],
                member_id=p["member_id"],
                payment_date=p.get("payment_date") or "",
                payment_type=p["payment_type"],
                payment_purpose=p.get("payment_purpose") or "",
                period_covered=p.get("period_covered") or "",
                amount=float(p["amount"]),
                receipt_number=p.get("receipt_number") or "",
                voucher=p.get("voucher") or "",
                balance=float(p["balance"]) if p.get("balance") is not None else None,
                payment_method=p.get("payment_method") or "",
                notes=p.get("notes") or "",
                created_at=p.get("created_at") or _now_iso(),
            )
        )

    for d in seed.get("monthly_dues", []):
        _db.session.add(
            OfficeMonthlyDue(
                id=d["id"],
                member_id=d["member_id"],
                period=d["period"],
                status=d.get("status") or "Unpaid",
                amount=float(d.get("amount") or 0.0),
                notes=d.get("notes") or "",
                created_at=d.get("created_at") or _now_iso(),
                updated_at=d.get("updated_at") or _now_iso(),
            )
        )

    for doc in seed.get("documents", []):
        _db.session.add(
            OfficeDocument(
                id=doc["id"],
                member_id=doc["member_id"],
                document_type=doc["document_type"],
                original_filename=doc["original_filename"],
                stored_filename=doc.get("stored_filename"),
                file_data=None,
                mime_type=doc.get("mime_type") or "",
                size_bytes=int(doc.get("size_bytes") or 0),
                issue_date=doc.get("issue_date") or "",
                expiry_date=doc.get("expiry_date") or "",
                created_at=doc.get("created_at") or _now_iso(),
            )
        )

    for batch in seed.get("import_batches", []):
        if not _db.session.get(OfficeImportBatch, batch["workbook_sha256"]):
            _db.session.add(
                OfficeImportBatch(
                    workbook_sha256=batch["workbook_sha256"],
                    imported_at=batch.get("imported_at") or _now_iso(),
                    member_count=int(batch.get("member_count") or 0),
                    payment_count=int(batch.get("payment_count") or 0),
                    dues_count=int(batch.get("dues_count") or 0),
                )
            )

    _db.session.commit()


def init_office_db(app, db, portal_member_model=None):
    """Define SQLAlchemy models, register blueprint, create tables, and seed initial data."""
    global _db, _portal_member_model
    _db = db
    if portal_member_model is not None:
        _portal_member_model = portal_member_model

    if not _models:

        class OfficeUser(db.Model):
            __tablename__ = "office_users"
            id = db.Column(db.String(36), primary_key=True)
            username = db.Column(db.String(50), unique=True, nullable=False)
            password_hash = db.Column(db.String(256), nullable=False)
            role = db.Column(db.String(20), nullable=False, default="Editor")
            created_at = db.Column(db.String(40), nullable=False)

        class OfficeSession(db.Model):
            __tablename__ = "office_sessions"
            token_hash = db.Column(db.String(64), primary_key=True)
            user_id = db.Column(
                db.String(36),
                db.ForeignKey("office_users.id", ondelete="CASCADE"),
                nullable=False,
            )
            expires_at = db.Column(db.BigInteger, nullable=False)

        class OfficeMember(db.Model):
            __tablename__ = "office_members"
            id = db.Column(db.String(36), primary_key=True)
            member_code = db.Column(db.String(100), unique=True, nullable=True)
            full_name = db.Column(db.String(255), nullable=False, index=True)
            address = db.Column(db.Text, nullable=False, default="")
            id_number = db.Column(db.String(255), nullable=False, default="")
            driver_license_number = db.Column(db.String(255), nullable=False, default="")
            unit_name = db.Column(db.String(255), nullable=False, default="")
            vehicle_make = db.Column(db.String(255), nullable=False, default="")
            vehicle_model = db.Column(db.String(255), nullable=False, default="")
            vehicle_year = db.Column(db.String(50), nullable=False, default="")
            plate_number = db.Column(db.String(255), nullable=False, default="", index=True)
            mv_file_number = db.Column(db.String(255), nullable=False, default="", index=True)
            franchise_case_number = db.Column(db.String(255), nullable=False, default="", index=True)
            batch_number = db.Column(db.String(255), nullable=False, default="", index=True)
            franchise_start_date = db.Column(db.String(20), nullable=False, default="")
            monthly_dues_amount = db.Column(db.Float, nullable=False, default=0.0)
            due_day = db.Column(db.Integer, nullable=True)
            status = db.Column(db.String(40), nullable=False, default="Active")
            moa_status = db.Column(db.String(255), nullable=False, default="")
            lease_status = db.Column(db.String(255), nullable=False, default="")
            pa_status = db.Column(db.String(40), nullable=False, default="Not recorded")
            cpc_status = db.Column(db.String(40), nullable=False, default="Not recorded")
            original_or_cr = db.Column(db.String(255), nullable=False, default="")
            facebook_name = db.Column(db.String(255), nullable=False, default="")
            terms = db.Column(db.String(255), nullable=False, default="")
            notes = db.Column(db.Text, nullable=False, default="")
            created_at = db.Column(db.String(40), nullable=False)
            updated_at = db.Column(db.String(40), nullable=False)

        class OfficePayment(db.Model):
            __tablename__ = "office_payments"
            id = db.Column(db.String(36), primary_key=True)
            member_id = db.Column(
                db.String(36),
                db.ForeignKey("office_members.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            )
            payment_date = db.Column(db.String(20), nullable=False, default="")
            payment_type = db.Column(db.String(80), nullable=False)
            payment_purpose = db.Column(db.String(255), nullable=False, default="")
            period_covered = db.Column(db.String(20), nullable=False, default="")
            amount = db.Column(db.Float, nullable=False)
            receipt_number = db.Column(db.String(100), nullable=False, default="")
            voucher = db.Column(db.String(100), nullable=False, default="")
            balance = db.Column(db.Float, nullable=True)
            payment_method = db.Column(db.String(80), nullable=False, default="")
            notes = db.Column(db.Text, nullable=False, default="")
            created_at = db.Column(db.String(40), nullable=False)

        class OfficeMonthlyDue(db.Model):
            __tablename__ = "office_monthly_dues"
            __table_args__ = (
                db.UniqueConstraint("member_id", "period", name="uq_office_dues_member_period"),
            )
            id = db.Column(db.String(36), primary_key=True)
            member_id = db.Column(
                db.String(36),
                db.ForeignKey("office_members.id", ondelete="CASCADE"),
                nullable=False,
            )
            period = db.Column(db.String(20), nullable=False, index=True)
            status = db.Column(db.String(40), nullable=False, default="Unpaid")
            amount = db.Column(db.Float, nullable=False, default=0.0)
            notes = db.Column(db.Text, nullable=False, default="")
            created_at = db.Column(db.String(40), nullable=False)
            updated_at = db.Column(db.String(40), nullable=False)

        class OfficeDocument(db.Model):
            __tablename__ = "office_documents"
            id = db.Column(db.String(36), primary_key=True)
            member_id = db.Column(
                db.String(36),
                db.ForeignKey("office_members.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            )
            document_type = db.Column(db.String(100), nullable=False)
            original_filename = db.Column(db.String(255), nullable=False)
            stored_filename = db.Column(db.String(255), unique=True, nullable=True)
            file_data = db.Column(db.LargeBinary, nullable=True)
            mime_type = db.Column(db.String(100), nullable=False, default="")
            size_bytes = db.Column(db.Integer, nullable=False, default=0)
            issue_date = db.Column(db.String(20), nullable=False, default="")
            expiry_date = db.Column(db.String(20), nullable=False, default="")
            created_at = db.Column(db.String(40), nullable=False)

        class OfficeImportBatch(db.Model):
            __tablename__ = "office_import_batches"
            workbook_sha256 = db.Column(db.String(64), primary_key=True)
            imported_at = db.Column(db.String(40), nullable=False)
            member_count = db.Column(db.Integer, nullable=False)
            payment_count = db.Column(db.Integer, nullable=False)
            dues_count = db.Column(db.Integer, nullable=False)

        _models.update(
            {
                "OfficeUser": OfficeUser,
                "OfficeSession": OfficeSession,
                "OfficeMember": OfficeMember,
                "OfficePayment": OfficePayment,
                "OfficeMonthlyDue": OfficeMonthlyDue,
                "OfficeDocument": OfficeDocument,
                "OfficeImportBatch": OfficeImportBatch,
            }
        )

    if "office_db" not in app.blueprints:
        app.register_blueprint(office_db_bp)

    with app.app_context():
        db.create_all()
        try:
            if _models["OfficeUser"].query.count() == 0:
                default_admin = _models["OfficeUser"](
                    id=str(uuid.uuid4()),
                    username="admin",
                    password_hash=generate_password_hash("enasia2026"),
                    role="Admin",
                    created_at=_now_iso(),
                )
                db.session.add(default_admin)
                db.session.commit()
        except Exception:
            db.session.rollback()
        try:
            _seed_office_db_if_empty(app)
        except Exception:
            db.session.rollback()
        try:
            sync_all_office_members_to_portal()
        except Exception:
            db.session.rollback()
