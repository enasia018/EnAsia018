import csv
import json
import os
import re
import smtplib
import uuid
import zipfile
import mimetypes
import xml.etree.ElementTree as ET
from io import BytesIO, StringIO
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "enasia_secret_key")

database_url = os.environ.get("DATABASE_URL")

if database_url:
    if database_url.startswith("postgres://"):
        database_url = database_url.replace(
            "postgres://", "postgresql+psycopg://", 1
        )
    elif database_url.startswith("postgresql://"):
        database_url = database_url.replace(
            "postgresql://", "postgresql+psycopg://", 1
        )

app.config["SQLALCHEMY_DATABASE_URI"] = database_url or "sqlite:///reservations.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

db = SQLAlchemy(app)

BUSINESS_TIMEZONE = ZoneInfo("Asia/Manila")


def reservation_period(reservation):
    try:
        start_date = datetime.strptime(reservation.pickup_date, "%Y-%m-%d").date()
        days = max(int(reservation.days), 1)
    except (TypeError, ValueError):
        return None
    return start_date, start_date + timedelta(days=days)


def van_manual_period(van):
    if not getattr(van, "schedule_date", None):
        return None
    try:
        start_date = datetime.strptime(van.schedule_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    days = None
    end_str = getattr(van, "schedule_end_date", None)
    if end_str:
        try:
            end_inclusive = datetime.strptime(end_str, "%Y-%m-%d").date()
            if end_inclusive >= start_date:
                days = (end_inclusive - start_date).days + 1
        except (TypeError, ValueError):
            pass
    if days is None:
        try:
            days = max(int(getattr(van, "schedule_days", None) or 1), 1)
        except (TypeError, ValueError):
            days = 1
    return start_date, start_date + timedelta(days=days)


def find_reservation_conflict(van_name, start_date, days, exclude_reservation_id=None):
    new_end_date = start_date + timedelta(days=days)
    approved_reservations = Reservation.query.filter_by(
        vans=van_name,
        status="Approved",
    ).all()

    for reservation in approved_reservations:
        if reservation.id == exclude_reservation_id:
            continue
        period = reservation_period(reservation)
        if period is None:
            continue
        booked_start, booked_end = period
        if start_date < booked_end and booked_start < new_end_date:
            return reservation

    van_obj = Van.query.filter_by(name=van_name).first()
    if van_obj is not None:
        m_period = van_manual_period(van_obj)
        if m_period is not None:
            m_start, m_end = m_period
            if start_date < m_end and m_start < new_end_date:
                return van_obj
    return None


def get_van_display_status(van, reservations, today=None):
    if van.status != "Available":
        return "Not Available"

    today = today or datetime.now(BUSINESS_TIMEZONE).date()
    reserved_through = today + timedelta(days=3)
    m_period = van_manual_period(van)
    if m_period is not None:
        m_start, m_end = m_period
        if m_start <= reserved_through and m_end > today:
            return "Not Available"

    for reservation in reservations:
        if reservation.vans != van.name or reservation.status != "Approved":
            continue
        period = reservation_period(reservation)
        if period is None:
            continue
        booked_start, booked_end = period
        if booked_start <= reserved_through and booked_end > today:
            return "Not Available"
    return "Available"


UPLOAD_FOLDER = os.environ.get(
    "UPLOAD_FOLDER",
    os.path.join(app.root_path, "static", "uploads"),
)
app.config["UPLOAD_FOLDER"] = os.path.abspath(UPLOAD_FOLDER)
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)


class Reservation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    address = db.Column(db.String(200), nullable=False)
    contact = db.Column(db.String(50), nullable=False)
    vans = db.Column(db.String(50), nullable=False)
    pickup = db.Column(db.String(100), nullable=False)
    destination = db.Column(db.String(200), nullable=True)
    pickup_date = db.Column(db.String(10), nullable=True)
    pickup_time = db.Column(db.String(5), nullable=True)
    dropoff = db.Column(db.String(100), nullable=False)
    days = db.Column(db.String(20), nullable=False)
    nights = db.Column(db.String(20), nullable=False)
    receipt = db.Column(db.String(200), nullable=True)
    receipt_data = db.Column(db.LargeBinary, nullable=True)
    receipt_mimetype = db.Column(db.String(150), nullable=True)
    status = db.Column(db.String(50), default="Pending")


class Van(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    plate_number = db.Column(db.String(50), nullable=True)
    driver_name = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(50), default="Available")
    schedule_date = db.Column(db.String(10), nullable=True)
    schedule_end_date = db.Column(db.String(10), nullable=True)
    schedule_days = db.Column(db.Integer, nullable=True)
    schedule_time = db.Column(db.String(5), nullable=True)
    schedule_note = db.Column(db.String(500), nullable=True)


class DestinationRate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pickup_location = db.Column(db.String(100), nullable=False)
    destination = db.Column(db.String(100), nullable=False)
    rate_per_day = db.Column(db.Float, nullable=False)


class FranchiseOption(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    details = db.Column(db.Text, nullable=False)


class FranchiseApplication(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(100), nullable=False)
    address = db.Column(db.String(200), nullable=False)
    contact = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(100), nullable=True)
    vehicle_type = db.Column(db.String(50), nullable=False)
    vehicle_model = db.Column(db.String(50), nullable=False)
    plate_number = db.Column(db.String(50), nullable=False)
    # Keep the original filenames for compatibility with existing records.
    # New OR/CR uploads are stored directly in Neon PostgreSQL as binary data.
    or_file = db.Column(db.String(200), nullable=False)
    cr_file = db.Column(db.String(200), nullable=False)
    or_file_data = db.Column(db.LargeBinary, nullable=True)
    cr_file_data = db.Column(db.LargeBinary, nullable=True)
    or_file_mimetype = db.Column(db.String(150), nullable=True)
    cr_file_mimetype = db.Column(db.String(150), nullable=True)
    status = db.Column(db.String(50), default="Pending")
    schedule = db.Column(db.String(100), nullable=True)


class TourApplication(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(100), nullable=False)
    address = db.Column(db.String(200), nullable=False)
    contact = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(100), nullable=True)
    tour_package = db.Column(db.String(200), nullable=False)
    tour_date = db.Column(db.String(50), nullable=False)
    pax = db.Column(db.Integer, nullable=False)
    receipt = db.Column(db.String(200), nullable=True)
    receipt_data = db.Column(db.LargeBinary, nullable=True)
    receipt_mimetype = db.Column(db.String(150), nullable=True)
    status = db.Column(db.String(50), default="Pending")


class GCashPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(100), nullable=False)
    contact = db.Column(db.String(50), nullable=False)
    receipt = db.Column(db.String(200), nullable=False)
    receipt_data = db.Column(db.LargeBinary, nullable=True)
    receipt_mimetype = db.Column(db.String(150), nullable=True)
    status = db.Column(db.String(50), default="Pending")


DEFAULT_MEMBER_PASSWORD = "Password123"


class Member(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_code = db.Column(db.String(30), unique=True, nullable=False)  # e.g. ENS-00017
    full_name = db.Column(db.String(150), nullable=False)
    status = db.Column(db.String(50), default="Active")
    address = db.Column(db.String(250), nullable=True)
    member_id_no = db.Column(db.String(100), nullable=True)
    driver_license = db.Column(db.String(100), nullable=True)
    unit_name = db.Column(db.String(100), nullable=True)
    vehicle = db.Column(db.String(100), nullable=True)
    plate_number = db.Column(db.String(50), nullable=True)
    mv_file_number = db.Column(db.String(100), nullable=True)
    franchise_case = db.Column(db.String(100), nullable=True)
    batch_number = db.Column(db.String(100), nullable=True)
    franchise_start_date = db.Column(db.String(50), nullable=True)
    monthly_dues = db.Column(db.String(50), nullable=True, default="₱0.00")
    due_day = db.Column(db.String(50), nullable=True)
    moa_status = db.Column(db.String(100), nullable=True)
    lease_status = db.Column(db.String(100), nullable=True)
    pa_status = db.Column(db.String(100), nullable=True, default="Not recorded")
    cpc_status = db.Column(db.String(100), nullable=True, default="Not recorded")
    original_or_cr = db.Column(db.String(100), nullable=True)
    facebook_name = db.Column(db.String(150), nullable=True)
    terms = db.Column(db.String(100), nullable=True)
    password_hash = db.Column(db.String(256), nullable=False)
    must_change_password = db.Column(db.Boolean, default=True, nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class VanTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    van_id = db.Column(db.Integer, nullable=False)
    van_name = db.Column(db.String(100), nullable=False)
    tx_type = db.Column(db.String(20), nullable=False)  # "earning" or "expense"
    tx_date = db.Column(db.String(10), nullable=False)  # YYYY-MM-DD
    tx_month = db.Column(db.String(7), nullable=False)  # YYYY-MM
    category = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(300), nullable=True)
    amount = db.Column(db.Float, nullable=False, default=0.0)
    reservation_id = db.Column(db.Integer, nullable=True)


def generate_next_member_code():
    existing_codes = [m.member_code for m in Member.query.all() if m.member_code]
    max_num = 0
    for code in existing_codes:
        match = re.search(r"(\d+)$", code)
        if match:
            max_num = max(max_num, int(match.group(1)))
    return f"ENS-{max_num + 1:05d}"


def sync_local_seed_file():
    """When testing locally (no DATABASE_URL), keep seed_members.json synced with local edits."""
    if database_url:
        return
    try:
        seed_file = os.path.join(app.root_path, "seed_members.json")
        all_members = Member.query.order_by(Member.id).all()
        data = []
        for m in all_members:
            data.append({
                "member_code": m.member_code,
                "full_name": m.full_name,
                "status": m.status or "Active",
                "address": m.address or "",
                "member_id_no": m.member_id_no or "",
                "driver_license": m.driver_license or "",
                "unit_name": m.unit_name or "",
                "vehicle": m.vehicle or "",
                "plate_number": m.plate_number or "",
                "mv_file_number": m.mv_file_number or "",
                "franchise_case": m.franchise_case or "",
                "batch_number": m.batch_number or "",
                "franchise_start_date": m.franchise_start_date or "",
                "monthly_dues": m.monthly_dues or "₱0.00",
                "due_day": m.due_day or "",
                "moa_status": m.moa_status or "",
                "lease_status": m.lease_status or "",
                "pa_status": m.pa_status or "Not recorded",
                "cpc_status": m.cpc_status or "Not recorded",
                "original_or_cr": m.original_or_cr or "",
                "facebook_name": m.facebook_name or "",
                "terms": m.terms or "",
            })
        with open(seed_file, "w", encoding="utf-8") as sf:
            json.dump(data, sf, ensure_ascii=False, indent=2)
    except Exception:
        pass


with app.app_context():
    db.create_all()

    # db.create_all() does not add columns to an existing table, so add the
    # document fields safely for existing SQLite and PostgreSQL databases.
    franchise_columns = {
        column["name"]
        for column in inspect(db.engine).get_columns("franchise_application")
    }
    binary_column_type = "BYTEA" if db.engine.dialect.name == "postgresql" else "BLOB"
    with db.engine.begin() as connection:
        if "or_file_data" not in franchise_columns:
            connection.execute(
                text(f"ALTER TABLE franchise_application ADD COLUMN or_file_data {binary_column_type}")
            )
        if "cr_file_data" not in franchise_columns:
            connection.execute(
                text(f"ALTER TABLE franchise_application ADD COLUMN cr_file_data {binary_column_type}")
            )
        if "or_file_mimetype" not in franchise_columns:
            connection.execute(
                text("ALTER TABLE franchise_application ADD COLUMN or_file_mimetype VARCHAR(150)")
            )
        if "cr_file_mimetype" not in franchise_columns:
            connection.execute(
                text("ALTER TABLE franchise_application ADD COLUMN cr_file_mimetype VARCHAR(150)")
            )

    reservation_columns = {
        column["name"] for column in inspect(db.engine).get_columns("reservation")
    }
    with db.engine.begin() as connection:
        if "pickup_date" not in reservation_columns:
            connection.execute(
                text("ALTER TABLE reservation ADD COLUMN pickup_date VARCHAR(10)")
            )
        if "pickup_time" not in reservation_columns:
            connection.execute(
                text("ALTER TABLE reservation ADD COLUMN pickup_time VARCHAR(5)")
            )
        if "destination" not in reservation_columns:
            connection.execute(
                text("ALTER TABLE reservation ADD COLUMN destination VARCHAR(200)")
            )
        if "receipt_data" not in reservation_columns:
            connection.execute(
                text(f"ALTER TABLE reservation ADD COLUMN receipt_data {binary_column_type}")
            )
        if "receipt_mimetype" not in reservation_columns:
            connection.execute(
                text("ALTER TABLE reservation ADD COLUMN receipt_mimetype VARCHAR(150)")
            )

    tour_columns = {
        column["name"] for column in inspect(db.engine).get_columns("tour_application")
    }
    with db.engine.begin() as connection:
        if "receipt_data" not in tour_columns:
            connection.execute(
                text(f"ALTER TABLE tour_application ADD COLUMN receipt_data {binary_column_type}")
            )
        if "receipt_mimetype" not in tour_columns:
            connection.execute(
                text("ALTER TABLE tour_application ADD COLUMN receipt_mimetype VARCHAR(150)")
            )

    gcash_columns = {
        column["name"] for column in inspect(db.engine).get_columns("g_cash_payment")
    }
    with db.engine.begin() as connection:
        if "receipt_data" not in gcash_columns:
            connection.execute(
                text(f"ALTER TABLE g_cash_payment ADD COLUMN receipt_data {binary_column_type}")
            )
        if "receipt_mimetype" not in gcash_columns:
            connection.execute(
                text("ALTER TABLE g_cash_payment ADD COLUMN receipt_mimetype VARCHAR(150)")
            )

    van_columns = {column["name"] for column in inspect(db.engine).get_columns("van")}
    with db.engine.begin() as connection:
        if "schedule_date" not in van_columns:
            connection.execute(text("ALTER TABLE van ADD COLUMN schedule_date VARCHAR(10)"))
        if "schedule_time" not in van_columns:
            connection.execute(text("ALTER TABLE van ADD COLUMN schedule_time VARCHAR(5)"))
        if "schedule_note" not in van_columns:
            connection.execute(text("ALTER TABLE van ADD COLUMN schedule_note VARCHAR(500)"))
        if "schedule_end_date" not in van_columns:
            connection.execute(text("ALTER TABLE van ADD COLUMN schedule_end_date VARCHAR(10)"))
        if "schedule_days" not in van_columns:
            connection.execute(text("ALTER TABLE van ADD COLUMN schedule_days INTEGER"))

    member_columns = {column["name"] for column in inspect(db.engine).get_columns("member")}
    if "must_change_password" not in member_columns:
        try:
            with db.engine.begin() as connection:
                if db.engine.dialect.name == "postgresql":
                    connection.execute(
                        text("ALTER TABLE member ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT TRUE")
                    )
                else:
                    connection.execute(
                        text("ALTER TABLE member ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT 1")
                    )
        except Exception:
            pass

    if not Van.query.first():
        db.session.add(
            Van(
                name="Toyota Hiace",
                plate_number="ABC-1234",
                driver_name="Juan Dela Cruz",
                status="Available",
            )
        )
        db.session.add(
            Van(
                name="Nissan Urvan",
                plate_number="XYZ-5678",
                driver_name="Pedro Santos",
                status="Available",
            )
        )
        db.session.commit()

    if not FranchiseOption.query.first():
        db.session.add(
            FranchiseOption(
                title="Option 1: Full Cash Payment",
                details="Pay the full amount upfront with a special discount and instant approval.",
            )
        )
        db.session.add(
            FranchiseOption(
                title="Option 2: Installment Plan",
                details="Pay a down payment and split the rest into manageable monthly installments.",
            )
        )
        db.session.add(
            FranchiseOption(
                title="Option 3: Revenue Sharing",
                details="Partner with us by sharing a percentage of your monthly earnings.",
            )
        )
        db.session.commit()

    if not Member.query.first():
        seed_file = os.path.join(app.root_path, "seed_members.json")
        if os.path.isfile(seed_file):
            with open(seed_file, "r", encoding="utf-8") as sf:
                seed_data = json.load(sf)
            for item in seed_data:
                m = Member(
                    member_code=item["member_code"],
                    full_name=item["full_name"],
                    status=item.get("status") or "Active",
                    address=item.get("address") or None,
                    member_id_no=item.get("member_id_no") or None,
                    driver_license=item.get("driver_license") or None,
                    unit_name=item.get("unit_name") or None,
                    vehicle=item.get("vehicle") or None,
                    plate_number=item.get("plate_number") or None,
                    mv_file_number=item.get("mv_file_number") or None,
                    franchise_case=item.get("franchise_case") or None,
                    batch_number=item.get("batch_number") or None,
                    franchise_start_date=item.get("franchise_start_date") or None,
                    monthly_dues=item.get("monthly_dues") or "₱0.00",
                    due_day=item.get("due_day") or None,
                    moa_status=item.get("moa_status") or None,
                    lease_status=item.get("lease_status") or None,
                    pa_status=item.get("pa_status") or "Not recorded",
                    cpc_status=item.get("cpc_status") or "Not recorded",
                    original_or_cr=item.get("original_or_cr") or None,
                    facebook_name=item.get("facebook_name") or None,
                    terms=item.get("terms") or None,
                    must_change_password=True,
                )
                m.set_password(DEFAULT_MEMBER_PASSWORD)
                db.session.add(m)
            db.session.commit()

    # Automatically migrate any members still using the legacy 'enasia2026' default
    # password to 'Password123' and require password change upon first login.
    # Uses a single pre-computed hash and quick first-row check so startup is instant on Gunicorn/NeonDB.
    try:
        first_member = Member.query.order_by(Member.id).first()
        if first_member and first_member.check_password("enasia2026"):
            default_pw_hash = generate_password_hash(DEFAULT_MEMBER_PASSWORD)
            for existing_member in Member.query.all():
                if existing_member.check_password("enasia2026"):
                    existing_member.password_hash = default_pw_hash
                    existing_member.must_change_password = True
            db.session.commit()
    except Exception:
        db.session.rollback()

from office_db import (
    init_office_db,
    _models as office_models,
    _now_iso,
    _sync_office_member_to_portal,
    sync_all_office_members_to_portal,
    sync_portal_member_to_office,
)

init_office_db(app, db, portal_member_model=Member)

ADMIN_USER = "admin"
ADMIN_PASS = "enasia2026"
STAFF_ROLES = ("Admin", "Editor", "View only")


@app.before_request
def enforce_admin_role_permissions():
    """Enforce View-only and Editor role restrictions across /admin routes."""
    path = request.path
    if not path.startswith("/admin") or path.startswith("/admin/database"):
        return None
    if path == "/admin" or path == "/admin/logout" or path == "/admin/change-my-password":
        return None
    if not session.get("logged_in"):
        return None

    role = session.get("admin_role", "Admin")
    # Admin-only user management routes
    if path.startswith("/admin/users") and role != "Admin":
        flash("Only an Administrator can manage staff user accounts.", "error")
        return redirect(request.referrer or url_for("admin_dashboard"))

    # View-only accounts cannot perform any modifying actions (POST or GET approve/delete/toggle)
    if role == "View only":
        is_modifying_get = (
            path.startswith("/admin/approve/")
            or path.startswith("/admin/tour/delete/")
            or path.startswith("/admin/van/toggle/")
        )
        if request.method != "GET" or is_modifying_get:
            flash("View-only accounts cannot add, edit, approve, or delete records.", "error")
            return redirect(request.referrer or url_for("admin_dashboard"))
    return None


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/franchise")
def franchise():
    all_options = FranchiseOption.query.all()
    return render_template("franchise.html", options=all_options)


@app.route("/franchise/apply/<int:option_id>", methods=["GET", "POST"])
def franchise_apply(option_id):
    if request.method == "POST":
        full_name = request.form.get("full_name")
        address = request.form.get("address")
        contact = request.form.get("contact")
        email = request.form.get("email")
        vehicle_type = request.form.get("vehicle_type")
        vehicle_model = request.form.get("vehicle_model")
        plate_number = request.form.get("plate_number")

        # Store OR/CR file bytes in the database instead of SnapDeploy's
        # temporary local filesystem. The filenames remain for admin display
        # and compatibility with older application records.
        or_file = request.files.get("or_file")
        or_filename = ""
        or_file_data = None
        or_file_mimetype = None
        if or_file and or_file.filename != "":
            safe_name = secure_filename(or_file.filename)
            or_filename = f"or{option_id}_{uuid.uuid4().hex}_{safe_name}"[:200]
            or_file_data = or_file.read()
            or_file_mimetype = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"

        cr_file = request.files.get("cr_file")
        cr_filename = ""
        cr_file_data = None
        cr_file_mimetype = None
        if cr_file and cr_file.filename != "":
            safe_name = secure_filename(cr_file.filename)
            cr_filename = f"cr{option_id}_{uuid.uuid4().hex}_{safe_name}"[:200]
            cr_file_data = cr_file.read()
            cr_file_mimetype = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"

        new_app = FranchiseApplication(
            full_name=full_name,
            address=address,
            contact=contact,
            email=email,
            vehicle_type=vehicle_type,
            vehicle_model=vehicle_model,
            plate_number=plate_number,
            or_file=or_filename,
            cr_file=cr_filename,
            or_file_data=or_file_data,
            cr_file_data=cr_file_data,
            or_file_mimetype=or_file_mimetype,
            cr_file_mimetype=cr_file_mimetype,
            status=f"Pending (Option {option_id})"
        )
        db.session.add(new_app)
        db.session.commit()

        return render_template("franchise_apply.html", success=True, option_id=option_id)

    return render_template("franchise_apply.html", success=False, option_id=option_id)


@app.route("/vans-list")
def vans_list():
    all_vans = Van.query.all()
    approved_reservations = Reservation.query.filter_by(status="Approved").order_by(
        Reservation.pickup_date, Reservation.pickup_time
    ).all()
    reservations_by_van = {}
    for reservation in approved_reservations:
        period = reservation_period(reservation)
        if period is None:
            continue
        start_date, end_date = period
        if end_date <= datetime.now(BUSINESS_TIMEZONE).date():
            continue
        reservations_by_van.setdefault(reservation.vans, []).append(
            {
                "start_date": start_date.isoformat(),
                "last_reserved_date": (end_date - timedelta(days=1)).isoformat(),
                "pickup_time": reservation.pickup_time,
                "days": reservation.days,
            }
        )
    for van in all_vans:
        m_period = van_manual_period(van)
        if m_period is not None:
            m_start, m_end = m_period
            if m_end > datetime.now(BUSINESS_TIMEZONE).date():
                reservations_by_van.setdefault(van.name, []).append(
                    {
                        "start_date": m_start.isoformat(),
                        "last_reserved_date": (m_end - timedelta(days=1)).isoformat(),
                        "pickup_time": van.schedule_time,
                        "days": (m_end - m_start).days,
                    }
                )
    van_statuses = {
        van.id: get_van_display_status(van, approved_reservations)
        for van in all_vans
    }

    return render_template(
        "vans_list.html",
        vans=all_vans,
        reservations_by_van=reservations_by_van,
        van_statuses=van_statuses,
    )


@app.route("/tour-package")
def tour_package():
    return render_template("tour_packages.html")


@app.route("/tour-package/<package_slug>")
def tour_package_detail(package_slug):
    packages = {
        "casiguran-aurora-casapsapan-beach": {
            "title": "Casiguran Aurora x Casapsapan Beach (High Roof Van)",
            "available": True,
            "template": "tour_package.html",
        },
        "baguio": {
            "title": "Baguio",
            "available": True,
            "template": "tour_package_baguio.html",
        },
        "ilocos": {
            "title": "Ilocos Sur + Ilocos Norte",
            "available": True,
            "template": "tour_package_ilocos.html",
        },
        "la-union": {"title": "La Union", "available": False},
        "jomalig-island": {
            "title": "Jomalig Island",
            "available": True,
            "template": "tour_package_jomalig.html",
        },
        "calayan": {
            "title": "Calayan Island",
            "available": True,
            "template": "tour_package_calayan.html",
        },
        "kalanggaman-tacloban-matnog-albay": {
            "title": "Kalanggaman Island, Tacloban, Pink Beach Matnog + Albay",
            "available": True,
            "template": "tour_package_kalanggaman.html",
        },
        "burias-island": {
            "title": "Burias Island",
            "available": True,
            "template": "tour_package_burias.html",
        },
        "bolinao-pangasinan": {
            "title": "Bolinao Pangasinan",
            "available": True,
            "template": "tour_package_bolinao.html",
        },
    }
    package = packages.get(package_slug)
    if package is None:
        return "Tour package not found", 404
    if package["available"]:
        return render_template(package["template"])
    return render_template("tour_package_coming_soon.html", package_title=package["title"])


@app.route("/tour/apply", methods=["GET", "POST"])
def tour_apply():
    if request.method == "POST":
        full_name = request.form.get("full_name")
        address = request.form.get("address")
        contact = request.form.get("contact")
        email = request.form.get("email")
        tour_package = request.form.get("tour_package")
        tour_date = request.form.get("tour_date")
        pax = request.form.get("pax")

        new_tour = TourApplication(
            full_name=full_name,
            address=address,
            contact=contact,
            email=email,
            tour_package=tour_package,
            tour_date=tour_date,
            pax=int(pax) if pax else 1,
            status="Pending"
        )
        db.session.add(new_tour)
        db.session.commit()

        return render_template("tour_apply.html", success=True)

    return render_template("tour_apply.html", success=False)


@app.route("/tour-gcash")
def tour_gcash():
    return render_template("tour_gcash.html", success=False)


@app.route("/upload_tour_receipt", methods=["POST"])
def upload_tour_receipt():
    sender_name = request.form.get("sender_name", "").strip().lower()
    file = request.files.get("receipt")

    if file and file.filename != "":
        safe_name = secure_filename(file.filename)
        filename = f"tour_receipt_{uuid.uuid4().hex[:8]}_{safe_name}"[:200]
        file_bytes = file.read()
        file_mimetype = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        try:
            with open(os.path.join(app.config["UPLOAD_FOLDER"], filename), "wb") as fp:
                fp.write(file_bytes)
        except Exception:
            pass

        tour_app = TourApplication.query.filter(
            db.func.lower(TourApplication.full_name) == sender_name
        ).order_by(TourApplication.id.desc()).first()

        if tour_app:
            tour_app.receipt = filename
            tour_app.receipt_data = file_bytes
            tour_app.receipt_mimetype = file_mimetype
            tour_app.status = "Receipt Uploaded"
            db.session.commit()

    return render_template("tour_gcash.html", success=True)


@app.route("/rental", methods=["GET", "POST"])
def rental():
    available_vans = Van.query.filter_by(status="Available").all()
    all_rates = DestinationRate.query.all()
    approved_reservations = Reservation.query.filter_by(status="Approved").all()
    schedules_by_van = {}
    for reservation in approved_reservations:
        period = reservation_period(reservation)
        if period is None:
            continue
        start_date, end_date = period
        if end_date <= datetime.now(BUSINESS_TIMEZONE).date():
            continue
        schedules_by_van.setdefault(reservation.vans, []).append(
            {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "time": reservation.pickup_time or "",
            }
        )
    for van in available_vans:
        m_period = van_manual_period(van)
        if m_period is not None:
            m_start, m_end = m_period
            if m_end > datetime.now(BUSINESS_TIMEZONE).date():
                schedules_by_van.setdefault(van.name, []).append(
                    {
                        "start": m_start.isoformat(),
                        "end": m_end.isoformat(),
                        "time": van.schedule_time or "",
                    }
                )

    if request.method == "POST":
        name = request.form.get("name")
        address = request.form.get("address")
        contact = request.form.get("contact")
        vans = request.form.get("vans")
        destination = request.form.get("destination", "").strip()
        if destination == "__other__":
            destination = request.form.get("other_destination", "").strip()
        pickup = request.form.get("pickup")
        pickup_date = request.form.get("pickup_date")
        pickup_time = request.form.get("pickup_time")
        dropoff = request.form.get("dropoff")
        days = request.form.get("days")

        selected_van = next(
            (van for van in available_vans if van.name == vans),
            None,
        )
        try:
            requested_start = datetime.strptime(pickup_date, "%Y-%m-%d").date()
            rental_days = int(days)
            if rental_days < 2:
                raise ValueError
            rental_nights = rental_days - 1
        except (TypeError, ValueError):
            requested_start = None
            rental_days = 0
            rental_nights = 0

        reservation_error = None
        if selected_van is None:
            reservation_error = "That van is not currently available for booking."
        elif requested_start is None:
            reservation_error = (
                "Enter a valid pickup date and book for at least 2 days."
            )
        elif find_reservation_conflict(vans, requested_start, rental_days):
            reservation_error = (
                "This van is already reserved for one or more of your selected dates. "
                "Please choose another van or date range."
            )

        if reservation_error:
            return render_template(
                "rental.html",
                success=False,
                vans_list=available_vans,
                rates=all_rates,
                schedules_by_van=schedules_by_van,
                reservation_error=reservation_error,
            )

        file = request.files.get("receipt")
        filename = ""
        receipt_data = None
        receipt_mimetype = None
        if file and file.filename != "":
            safe_name = secure_filename(file.filename)
            filename = f"rental_{uuid.uuid4().hex[:8]}_{safe_name}"[:200]
            receipt_data = file.read()
            receipt_mimetype = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
            try:
                with open(os.path.join(app.config["UPLOAD_FOLDER"], filename), "wb") as fp:
                    fp.write(receipt_data)
            except Exception:
                pass

        new_reservation = Reservation(
            name=name,
            address=address,
            contact=contact,
            vans=vans,
            pickup=pickup,
            destination=destination or None,
            pickup_date=pickup_date,
            pickup_time=pickup_time,
            dropoff=dropoff,
            days=rental_days,
            nights=rental_nights,
            receipt=filename,
            receipt_data=receipt_data,
            receipt_mimetype=receipt_mimetype,
            status="Pending",
        )
        db.session.add(new_reservation)
        db.session.commit()

        return render_template(
            "rental.html",
            success=True,
            vans_list=available_vans,
            rates=all_rates,
            schedules_by_van=schedules_by_van,
        )

    return render_template(
        "rental.html",
        success=False,
        vans_list=available_vans,
        rates=all_rates,
        schedules_by_van=schedules_by_van,
    )


@app.route("/status", methods=["GET", "POST"])
def check_status():
    result = None
    upload_success = session.pop('upload_success', False)

    if request.method == "POST":
        search_name = request.form.get("search_name", "").strip().lower()
        reservation = Reservation.query.filter(
            db.func.lower(Reservation.name) == search_name
        ).first()
        if reservation:
            result = reservation
        else:
            result = "not_found"

    return render_template("status.html", result=result, upload_success=upload_success)


@app.route("/gcash")
def gcash_page():
    return render_template("gcash.html", success=False)


@app.route("/submit-gcash-payment", methods=["POST"])
def submit_gcash_payment():
    full_name = request.form.get("full_name")
    contact = request.form.get("contact")
    file = request.files.get("receipt")

    if file and file.filename != "":
        safe_name = secure_filename(file.filename)
        filename = f"gcash_{uuid.uuid4().hex[:8]}_{safe_name}"[:200]
        file_bytes = file.read()
        file_mimetype = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        try:
            with open(os.path.join(app.config["UPLOAD_FOLDER"], filename), "wb") as fp:
                fp.write(file_bytes)
        except Exception:
            pass

        new_payment = GCashPayment(
            full_name=full_name,
            contact=contact,
            receipt=filename,
            receipt_data=file_bytes,
            receipt_mimetype=file_mimetype,
            status="Pending"
        )
        db.session.add(new_payment)
        db.session.commit()

    return render_template("gcash.html", success=True)


@app.route("/upload_receipt/<int:res_id>", methods=["POST"])
def upload_receipt(res_id):
    file = request.files.get("receipt")
    if file and file.filename != "":
        safe_name = secure_filename(file.filename)
        filename = f"receipt_{res_id}_{uuid.uuid4().hex[:6]}_{safe_name}"[:200]
        file_bytes = file.read()
        file_mimetype = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        try:
            with open(os.path.join(app.config["UPLOAD_FOLDER"], filename), "wb") as fp:
                fp.write(file_bytes)
        except Exception:
            pass

        reservation = db.session.get(Reservation, res_id)
        if reservation:
            reservation.receipt = filename
            reservation.receipt_data = file_bytes
            reservation.receipt_mimetype = file_mimetype
            db.session.commit()
            session['upload_success'] = True

            try:
                msg = EmailMessage()
                msg.set_subject(f"New Receipt from {reservation.name}")
                msg["From"] = "your_email@gmail.com"
                msg["To"] = "enriquezbilly06@gmail.com"
                
                email_body = f"Someone uploaded a receipt for the van rental.\n\nName: {reservation.name}\nVan: {reservation.vans}"
                msg.set_content(email_body)

                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                    smtp.login("your_email@gmail.com", "your_app_password")
                    smtp.send_message(msg)
            except Exception as e:
                print("Error sending email:", e)

    return redirect(url_for("check_status"))


def parse_xlsx_rows(file_bytes):
    """Pure-Python .xlsx reader (no external library needed)."""
    rows = []
    with zipfile.ZipFile(BytesIO(file_bytes)) as zf:
        shared_strings = []
        if "xl/sharedStrings.xml" in zf.namelist():
            ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in ss_root.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"):
                texts = [
                    t.text or ""
                    for t in si.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")
                ]
                shared_strings.append("".join(texts))

        sheet_files = sorted(
            name for name in zf.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        )
        if not sheet_files:
            return rows

        sheet_root = ET.fromstring(zf.read(sheet_files[0]))
        ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

        def col_to_idx(cell_ref):
            letters = "".join(ch for ch in cell_ref if ch.isalpha())
            idx = 0
            for ch in letters:
                idx = idx * 26 + (ord(ch.upper()) - ord("A") + 1)
            return idx - 1

        for row_el in sheet_root.findall(f".//{ns}row"):
            row_dict = {}
            max_col = -1
            for c_el in row_el.findall(f"{ns}c"):
                ref = c_el.attrib.get("r", "A1")
                c_idx = col_to_idx(ref)
                max_col = max(max_col, c_idx)
                c_type = c_el.attrib.get("t", "")
                v_el = c_el.find(f"{ns}v")
                is_el = c_el.find(f"{ns}is")
                val = ""
                if c_type == "s" and v_el is not None and v_el.text is not None:
                    s_idx = int(v_el.text)
                    val = shared_strings[s_idx] if 0 <= s_idx < len(shared_strings) else ""
                elif c_type == "inlineStr" and is_el is not None:
                    val = "".join(t.text or "" for t in is_el.findall(f".//{ns}t"))
                elif v_el is not None and v_el.text is not None:
                    val = v_el.text.strip()
                    if re.fullmatch(r"-?\d+\.0+", val):
                        val = val.split(".")[0]
                row_dict[c_idx] = val.strip()
            if max_col >= 0:
                rows.append([row_dict.get(i, "") for i in range(max_col + 1)])
    return rows


@app.route("/members", methods=["GET", "POST"])
def members():
    error = None
    member = None

    if request.method == "POST":
        login_id = request.form.get("member_id", "").strip()
        password = request.form.get("password", "")

        found = Member.query.filter(
            (db.func.upper(Member.member_code) == login_id.upper())
            | (
                (Member.member_id_no.isnot(None))
                & (Member.member_id_no != "")
                & (db.func.upper(Member.member_id_no) == login_id.upper())
            )
        ).first()

        if found and found.check_password(password):
            # Pull latest profile details from EnAsia Database System (OfficeMember) if present
            OfficeMember = office_models.get("OfficeMember")
            if OfficeMember is not None:
                om = OfficeMember.query.filter(
                    db.func.lower(OfficeMember.member_code) == (found.member_code or "").lower()
                ).first()
                if om:
                    _sync_office_member_to_portal(om)
                    db.session.commit()
            if found.check_password(DEFAULT_MEMBER_PASSWORD) or found.check_password("enasia2026"):
                found.must_change_password = True
                db.session.commit()
            session["member_logged_in_id"] = found.id
            member = found
        else:
            error = "Invalid Member ID or password. Please try again."
    elif session.get("member_logged_in_id"):
        member = db.session.get(Member, session.get("member_logged_in_id"))
        if member is None:
            session.pop("member_logged_in_id", None)
        else:
            OfficeMember = office_models.get("OfficeMember")
            if OfficeMember is not None:
                om = OfficeMember.query.filter(
                    db.func.lower(OfficeMember.member_code) == (member.member_code or "").lower()
                ).first()
                if om:
                    _sync_office_member_to_portal(om)
                    db.session.commit()

    return render_template("members.html", member=member, error=error)


@app.route("/members/logout")
def member_logout():
    session.pop("member_logged_in_id", None)
    return redirect(url_for("members"))


@app.route("/members/change-password", methods=["POST"])
def member_change_password():
    member_id = session.get("member_logged_in_id") or request.form.get("member_db_id", type=int)
    member = db.session.get(Member, member_id) if member_id else None
    if member is None:
        return redirect(url_for("members"))

    new_password = request.form.get("new_password", "").strip()
    confirm_password = request.form.get("confirm_password", "").strip()
    pw_error = None
    pw_success = None

    if not new_password or len(new_password) < 6:
        pw_error = "Please enter a new password of at least 6 characters."
    elif new_password != confirm_password:
        pw_error = "New password and confirmation do not match."
    elif new_password in (DEFAULT_MEMBER_PASSWORD, "enasia2026"):
        pw_error = f"Please choose a personal password different from the default ({DEFAULT_MEMBER_PASSWORD})."
    else:
        member.set_password(new_password)
        member.must_change_password = False
        db.session.commit()
        session["member_logged_in_id"] = member.id
        pw_success = "Your password has been updated! Your account is now secured."

    return render_template(
        "members.html",
        member=member,
        error=None,
        pw_error=pw_error,
        pw_success=pw_success,
    )


@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    error = None
    OfficeUser = office_models["OfficeUser"]
    if request.method == "POST":
        username_in = (request.form.get("username") or "").strip()
        password_in = request.form.get("password") or ""

        # Ensure default admin exists in OfficeUser table
        if OfficeUser.query.count() == 0:
            default_admin = OfficeUser(
                id=str(uuid.uuid4()),
                username=ADMIN_USER,
                password_hash=generate_password_hash(ADMIN_PASS),
                role="Admin",
                created_at=_now_iso(),
            )
            db.session.add(default_admin)
            db.session.commit()

        user = OfficeUser.query.filter(
            db.func.lower(OfficeUser.username) == username_in.lower()
        ).first()

        if user and check_password_hash(user.password_hash, password_in):
            session["logged_in"] = True
            session["admin_user_id"] = user.id
            session["admin_username"] = user.username
            session["admin_role"] = user.role
            session.pop("office_db_signed_out", None)
            return redirect(url_for("admin_dashboard"))
        else:
            error = "Invalid username or password!"
    return render_template("admin_login.html", error=error)


@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    OfficeUser = office_models["OfficeUser"]
    all_reservations = Reservation.query.all()
    all_vans = Van.query.all()
    all_rates = DestinationRate.query.all()
    all_options = FranchiseOption.query.all()
    all_franchise_apps = FranchiseApplication.query.all()
    all_tour_apps = TourApplication.query.all()
    all_gcash_payments = GCashPayment.query.all()
    staff_users = OfficeUser.query.order_by(db.func.lower(OfficeUser.username)).all()
    van_statuses = {
        van.id: get_van_display_status(van, all_reservations)
        for van in all_vans
    }
    return render_template(
        "admin_dashboard.html",
        reservations=all_reservations,
        vans=all_vans,
        rates=all_rates,
        franchise_options=all_options,
        franchise_apps=all_franchise_apps,
        tour_apps=all_tour_apps,
        gcash_payments=all_gcash_payments,
        van_statuses=van_statuses,
        staff_users=staff_users,
        current_username=session.get("admin_username", "admin"),
        current_role=session.get("admin_role", "Admin"),
    )


@app.route("/admin/users/add", methods=["POST"])
def admin_add_staff_user():
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    if session.get("admin_role", "Admin") != "Admin":
        flash("Only an Administrator can add user accounts.", "error")
        return redirect(url_for("admin_dashboard"))

    OfficeUser = office_models["OfficeUser"]
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()
    role = (request.form.get("role") or "Editor").strip()

    if not re.match(r"^[a-zA-Z0-9._-]{3,50}$", username):
        flash("Username must be 3-50 letters, numbers, dots, underscores, or hyphens.", "error")
        return redirect(url_for("admin_dashboard"))
    if len(password) < 6:
        flash("Password must be at least 6 characters.", "error")
        return redirect(url_for("admin_dashboard"))
    if role not in STAFF_ROLES:
        flash("Invalid role selected. Choose Admin, Editor, or View only.", "error")
        return redirect(url_for("admin_dashboard"))
    if OfficeUser.query.filter(db.func.lower(OfficeUser.username) == username.lower()).first():
        flash(f"Username '{username}' is already in use.", "error")
        return redirect(url_for("admin_dashboard"))

    new_user = OfficeUser(
        id=str(uuid.uuid4()),
        username=username,
        password_hash=generate_password_hash(password),
        role=role,
        created_at=_now_iso(),
    )
    db.session.add(new_user)
    db.session.commit()
    flash(f"Created user account '{username}' with role '{role}'.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<user_id>/reset-password", methods=["POST"])
def admin_reset_staff_password(user_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    if session.get("admin_role", "Admin") != "Admin":
        flash("Only an Administrator can reset user passwords.", "error")
        return redirect(url_for("admin_dashboard"))

    OfficeUser = office_models["OfficeUser"]
    user = db.session.get(OfficeUser, user_id)
    if user is None:
        flash("User account not found.", "error")
        return redirect(url_for("admin_dashboard"))

    new_password = (request.form.get("new_password") or "").strip()
    if len(new_password) < 6:
        flash("New password must be at least 6 characters.", "error")
        return redirect(url_for("admin_dashboard"))

    user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    flash(f"Password for '{user.username}' ({user.role}) has been updated.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<user_id>/role", methods=["POST"])
def admin_update_staff_role(user_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    if session.get("admin_role", "Admin") != "Admin":
        flash("Only an Administrator can change user roles.", "error")
        return redirect(url_for("admin_dashboard"))

    OfficeUser = office_models["OfficeUser"]
    user = db.session.get(OfficeUser, user_id)
    if user is None:
        flash("User account not found.", "error")
        return redirect(url_for("admin_dashboard"))

    new_role = (request.form.get("role") or "").strip()
    if new_role not in STAFF_ROLES:
        flash("Choose Admin, Editor, or View only.", "error")
        return redirect(url_for("admin_dashboard"))

    if user.role == "Admin" and new_role != "Admin":
        admin_count = OfficeUser.query.filter_by(role="Admin").count()
        if admin_count <= 1:
            flash("Cannot remove Admin role from the last remaining Administrator.", "error")
            return redirect(url_for("admin_dashboard"))

    user.role = new_role
    if session.get("admin_user_id") == user.id:
        session["admin_role"] = new_role
    db.session.commit()
    flash(f"Updated role for '{user.username}' to '{new_role}'.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<user_id>/delete", methods=["POST"])
def admin_delete_staff_user(user_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    if session.get("admin_role", "Admin") != "Admin":
        flash("Only an Administrator can delete user accounts.", "error")
        return redirect(url_for("admin_dashboard"))

    OfficeUser = office_models["OfficeUser"]
    OfficeSession = office_models["OfficeSession"]
    user = db.session.get(OfficeUser, user_id)
    if user is None:
        flash("User account not found.", "error")
        return redirect(url_for("admin_dashboard"))

    if user.role == "Admin":
        admin_count = OfficeUser.query.filter_by(role="Admin").count()
        if admin_count <= 1:
            flash("Cannot delete the last remaining Administrator account.", "error")
            return redirect(url_for("admin_dashboard"))

    uname = user.username
    OfficeSession.query.filter_by(user_id=user.id).delete()
    db.session.delete(user)
    db.session.commit()
    flash(f"Deleted user account '{uname}'.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/change-my-password", methods=["POST"])
def admin_change_my_password():
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    OfficeUser = office_models["OfficeUser"]
    current_username = session.get("admin_username", "admin")
    user = OfficeUser.query.filter(
        db.func.lower(OfficeUser.username) == current_username.lower()
    ).first()
    if user is None:
        flash("Account not found.", "error")
        return redirect(url_for("admin_dashboard"))

    current_pw = request.form.get("current_password") or ""
    new_pw = (request.form.get("new_password") or "").strip()
    confirm_pw = (request.form.get("confirm_password") or "").strip()

    if not check_password_hash(user.password_hash, current_pw):
        flash("Current password is incorrect.", "error")
        return redirect(url_for("admin_dashboard"))
    if len(new_pw) < 6:
        flash("New password must be at least 6 characters.", "error")
        return redirect(url_for("admin_dashboard"))
    if new_pw != confirm_pw:
        flash("New password and confirmation do not match.", "error")
        return redirect(url_for("admin_dashboard"))

    user.password_hash = generate_password_hash(new_pw)
    db.session.commit()
    flash("Your login password has been changed successfully.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/members")
def admin_members_page():
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    all_members = Member.query.order_by(db.func.upper(Member.full_name)).all()
    selected_id = request.args.get("selected_id", type=int)
    selected_member = None
    if selected_id:
        selected_member = db.session.get(Member, selected_id)
    if selected_member is None and all_members:
        selected_member = all_members[0]
    next_code = generate_next_member_code()
    return render_template(
        "admin_members.html",
        members=all_members,
        selected_member=selected_member,
        next_code=next_code,
    )


@app.route("/admin/members/add", methods=["POST"])
def admin_add_member():
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    member_code = request.form.get("member_code", "").strip().upper()
    if not member_code:
        member_code = generate_next_member_code()

    full_name = request.form.get("full_name", "").strip().upper()
    password = request.form.get("password", "").strip()

    if not full_name or not password:
        flash("Full name and password are required.", "error")
        return redirect(url_for("admin_members_page"))

    if Member.query.filter(db.func.upper(Member.member_code) == member_code).first():
        flash(f"Member code '{member_code}' already exists.", "error")
        return redirect(url_for("admin_members_page"))

    new_member = Member(
        member_code=member_code,
        full_name=full_name,
        status=request.form.get("status", "Active").strip() or "Active",
        address=request.form.get("address", "").strip() or None,
        member_id_no=request.form.get("member_id_no", "").strip() or None,
        driver_license=request.form.get("driver_license", "").strip() or None,
        unit_name=request.form.get("unit_name", "").strip() or None,
        vehicle=request.form.get("vehicle", "").strip() or None,
        plate_number=request.form.get("plate_number", "").strip() or None,
        mv_file_number=request.form.get("mv_file_number", "").strip() or None,
        franchise_case=request.form.get("franchise_case", "").strip() or None,
        batch_number=request.form.get("batch_number", "").strip() or None,
        franchise_start_date=request.form.get("franchise_start_date", "").strip() or None,
        monthly_dues=request.form.get("monthly_dues", "").strip() or "₱0.00",
        due_day=request.form.get("due_day", "").strip() or None,
        moa_status=request.form.get("moa_status", "").strip() or None,
        lease_status=request.form.get("lease_status", "").strip() or None,
        pa_status=request.form.get("pa_status", "").strip() or "Not recorded",
        cpc_status=request.form.get("cpc_status", "").strip() or "Not recorded",
        original_or_cr=request.form.get("original_or_cr", "").strip() or None,
        facebook_name=request.form.get("facebook_name", "").strip() or None,
        terms=request.form.get("terms", "").strip() or None,
        must_change_password=True,
    )
    new_member.set_password(password)
    db.session.add(new_member)
    sync_portal_member_to_office(new_member)
    db.session.commit()
    sync_local_seed_file()
    flash(f"Member {full_name} ({member_code}) added successfully.", "success")
    return redirect(url_for("admin_members_page", selected_id=new_member.id))


@app.route("/admin/members/edit/<int:member_db_id>", methods=["POST"])
def admin_edit_member(member_db_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    member = db.session.get(Member, member_db_id)
    if member is None:
        flash("Member not found.", "error")
        return redirect(url_for("admin_members_page"))

    old_code = member.member_code
    new_code = request.form.get("member_code", "").strip().upper()
    if new_code and new_code != member.member_code:
        conflict = Member.query.filter(
            db.func.upper(Member.member_code) == new_code,
            Member.id != member.id,
        ).first()
        if conflict:
            flash(f"Member code '{new_code}' is already in use.", "error")
            return redirect(url_for("admin_members_page", selected_id=member.id))
        member.member_code = new_code

    member.full_name = request.form.get("full_name", member.full_name).strip().upper()
    member.status = request.form.get("status", member.status).strip() or "Active"
    member.address = request.form.get("address", "").strip() or None
    member.member_id_no = request.form.get("member_id_no", "").strip() or None
    member.driver_license = request.form.get("driver_license", "").strip() or None
    member.unit_name = request.form.get("unit_name", "").strip() or None
    member.vehicle = request.form.get("vehicle", "").strip() or None
    member.plate_number = request.form.get("plate_number", "").strip() or None
    member.mv_file_number = request.form.get("mv_file_number", "").strip() or None
    member.franchise_case = request.form.get("franchise_case", "").strip() or None
    member.batch_number = request.form.get("batch_number", "").strip() or None
    member.franchise_start_date = request.form.get("franchise_start_date", "").strip() or None
    member.monthly_dues = request.form.get("monthly_dues", "").strip() or "₱0.00"
    member.due_day = request.form.get("due_day", "").strip() or None
    member.moa_status = request.form.get("moa_status", "").strip() or None
    member.lease_status = request.form.get("lease_status", "").strip() or None
    member.pa_status = request.form.get("pa_status", "").strip() or "Not recorded"
    member.cpc_status = request.form.get("cpc_status", "").strip() or "Not recorded"
    member.original_or_cr = request.form.get("original_or_cr", "").strip() or None
    member.facebook_name = request.form.get("facebook_name", "").strip() or None
    member.terms = request.form.get("terms", "").strip() or None

    new_password = request.form.get("password", "").strip()
    if new_password:
        member.set_password(new_password)
        member.must_change_password = True

    sync_portal_member_to_office(member, old_code=old_code)
    db.session.commit()
    sync_local_seed_file()
    flash(f"Updated profile for {member.full_name}.", "success")
    return redirect(url_for("admin_members_page", selected_id=member.id))


@app.route("/admin/members/reset-password/<int:member_db_id>", methods=["POST"])
def admin_reset_member_password(member_db_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    member = db.session.get(Member, member_db_id)
    if member is None:
        flash("Member not found.", "error")
        return redirect(url_for("admin_members_page"))

    new_password = request.form.get("new_password", "").strip()
    if not new_password:
        flash("Password cannot be empty.", "error")
        return redirect(url_for("admin_members_page", selected_id=member.id))

    member.set_password(new_password)
    member.must_change_password = True
    db.session.commit()
    flash(
        f"Password for {member.full_name} ({member.member_code}) has been reset. They will be prompted to change it upon first login.",
        "success",
    )
    return redirect(url_for("admin_members_page", selected_id=member.id))


@app.route("/admin/members/reset-all-passwords", methods=["POST"])
def admin_reset_all_member_passwords():
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    default_pw_hash = generate_password_hash(DEFAULT_MEMBER_PASSWORD)
    all_members = Member.query.all()
    for m in all_members:
        m.password_hash = default_pw_hash
        m.must_change_password = True
    db.session.commit()
    flash(
        f"Reset all {len(all_members)} member passwords to '{DEFAULT_MEMBER_PASSWORD}' (first-login password change required).",
        "success",
    )
    return redirect(url_for("admin_members_page"))


@app.route("/admin/members/delete/<int:member_db_id>", methods=["POST"])
def admin_delete_member(member_db_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    member = db.session.get(Member, member_db_id)
    if member is None:
        flash("Member not found.", "error")
        return redirect(url_for("admin_members_page"))

    name = member.full_name
    db.session.delete(member)
    db.session.commit()
    sync_local_seed_file()
    flash(f"Deleted member {name}.", "success")
    return redirect(url_for("admin_members_page"))


@app.route("/admin/members/export")
def admin_export_members():
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    all_members = Member.query.order_by(db.func.upper(Member.full_name)).all()
    output = StringIO()
    output.write("\ufeff")  # UTF-8 BOM for Excel
    writer = csv.writer(output)
    writer.writerow([
        "Member Code",
        "Full Name",
        "Status",
        "Address",
        "Member ID",
        "Driver License",
        "Unit Name",
        "Vehicle",
        "Plate Number",
        "MV File Number",
        "Franchise Case",
        "Batch Number",
        "Franchise Start Date",
        "Monthly Dues",
        "Due Day",
        "MOA Status",
        "Lease Status",
        "PA Status",
        "CPC Status",
        "Original OR/CR",
        "Facebook Name",
        "Terms",
    ])
    for m in all_members:
        writer.writerow([
            m.member_code or "",
            m.full_name or "",
            m.status or "Active",
            m.address or "",
            m.member_id_no or "",
            m.driver_license or "",
            m.unit_name or "",
            m.vehicle or "",
            m.plate_number or "",
            m.mv_file_number or "",
            m.franchise_case or "",
            m.batch_number or "",
            m.franchise_start_date or "",
            m.monthly_dues or "₱0.00",
            m.due_day or "",
            m.moa_status or "",
            m.lease_status or "",
            m.pa_status or "Not recorded",
            m.cpc_status or "Not recorded",
            m.original_or_cr or "",
            m.facebook_name or "",
            m.terms or "",
        ])

    mem = BytesIO(output.getvalue().encode("utf-8"))
    return send_file(
        mem,
        mimetype="text/csv",
        as_attachment=True,
        download_name="enasia_members_export.csv",
    )


@app.route("/admin/members/import", methods=["POST"])
def admin_import_members():
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    file = request.files.get("excel_file")
    default_pw = request.form.get("default_password", DEFAULT_MEMBER_PASSWORD).strip() or DEFAULT_MEMBER_PASSWORD
    if not file or not file.filename:
        flash("Please choose an Excel (.xlsx) or CSV (.csv) file to import.", "error")
        return redirect(url_for("admin_members_page"))

    raw_bytes = file.read()
    filename_lower = file.filename.lower()
    rows = []
    try:
        if filename_lower.endswith(".xlsx"):
            rows = parse_xlsx_rows(raw_bytes)
        else:
            text_data = raw_bytes.decode("utf-8-sig", errors="replace")
            rows = list(csv.reader(StringIO(text_data)))
    except Exception as exc:
        flash(f"Could not read file: {exc}", "error")
        return redirect(url_for("admin_members_page"))

    if not rows or len(rows) < 2:
        flash("The uploaded file has no data rows.", "error")
        return redirect(url_for("admin_members_page"))

    headers = [re.sub(r"[^a-z0-9]+", "_", h.strip().lower()).strip("_") for h in rows[0]]

    def get_col(row_vals, *candidates):
        for cand in candidates:
            if cand in headers:
                idx = headers.index(cand)
                if idx < len(row_vals):
                    return row_vals[idx].strip()
        return ""

    added = 0
    updated = 0
    for r in rows[1:]:
        if not any(cell.strip() for cell in r):
            continue
        full_name = get_col(r, "full_name", "name", "member_name", "member").upper()
        if not full_name:
            continue
        code = get_col(r, "member_code", "code", "ens_code").upper()
        existing = None
        if code:
            existing = Member.query.filter(db.func.upper(Member.member_code) == code).first()
        if not existing:
            existing = Member.query.filter(db.func.upper(Member.full_name) == full_name).first()

        if existing:
            target = existing
            updated += 1
        else:
            if not code:
                code = generate_next_member_code()
            target = Member(member_code=code, full_name=full_name, must_change_password=True)
            target.set_password(get_col(r, "password") or default_pw)
            db.session.add(target)
            db.session.flush()
            added += 1

        target.status = get_col(r, "status") or target.status or "Active"
        target.address = get_col(r, "address") or target.address
        target.member_id_no = get_col(r, "member_id", "member_id_no") or target.member_id_no
        target.driver_license = get_col(r, "driver_license", "license") or target.driver_license
        target.unit_name = get_col(r, "unit_name", "unit") or target.unit_name
        target.vehicle = get_col(r, "vehicle", "unit_vehicle", "year_model") or target.vehicle
        target.plate_number = get_col(r, "plate_number", "plate") or target.plate_number
        target.mv_file_number = get_col(r, "mv_file_number", "mv_file") or target.mv_file_number
        target.franchise_case = get_col(r, "franchise_case", "case", "case_number") or target.franchise_case
        target.batch_number = get_col(r, "batch_number", "batch") or target.batch_number
        target.franchise_start_date = get_col(r, "franchise_start_date", "start_date") or target.franchise_start_date
        target.monthly_dues = get_col(r, "monthly_dues") or target.monthly_dues or "₱0.00"
        target.due_day = get_col(r, "due_day") or target.due_day
        target.moa_status = get_col(r, "moa_status", "moa") or target.moa_status
        target.lease_status = get_col(r, "lease_status", "lease") or target.lease_status
        target.pa_status = get_col(r, "pa_status", "pa") or target.pa_status or "Not recorded"
        target.cpc_status = get_col(r, "cpc_status", "cpc") or target.cpc_status or "Not recorded"
        target.original_or_cr = get_col(r, "original_or_cr", "or_cr") or target.original_or_cr
        target.facebook_name = get_col(r, "facebook_name", "facebook", "fb_name") or target.facebook_name
        target.terms = get_col(r, "terms") or target.terms
        row_pw = get_col(r, "password")
        if row_pw:
            target.set_password(row_pw)

    db.session.commit()
    flash(f"Import complete: {added} added, {updated} updated.", "success")
    return redirect(url_for("admin_members_page"))


@app.route("/admin/approve/<int:res_id>")
def approve_reservation(res_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    reservation = Reservation.query.get(res_id)
    if reservation:
        period = reservation_period(reservation)
        if period is not None:
            start_date, end_date = period
            conflict = find_reservation_conflict(
                reservation.vans,
                start_date,
                (end_date - start_date).days,
                exclude_reservation_id=reservation.id,
            )
            if conflict:
                flash(
                    "This reservation overlaps an already approved booking for the same van. "
                    "Update its van or schedule before approving.",
                    "error",
                )
                return redirect(url_for("admin_dashboard"))
        reservation.status = "Approved"
        db.session.commit()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/reservation/<int:res_id>/schedule", methods=["POST"])
def update_reservation_schedule(res_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    pickup_date = request.form.get("pickup_date", "").strip()
    pickup_time = request.form.get("pickup_time", "").strip()
    days_raw = request.form.get("days", "").strip()
    end_date_raw = request.form.get("end_date", "").strip()
    try:
        start_dt = datetime.strptime(pickup_date, "%Y-%m-%d").date()
        datetime.strptime(pickup_time, "%H:%M")
    except ValueError:
        flash("Enter a valid pickup date and time.", "error")
        return redirect(url_for("admin_dashboard"))

    reservation = db.session.get(Reservation, res_id)
    if reservation is None:
        flash("Reservation not found.", "error")
        return redirect(url_for("admin_dashboard"))

    rental_days = None
    if days_raw:
        try:
            rental_days = int(days_raw)
            if rental_days < 1:
                raise ValueError
        except ValueError:
            flash("Number of days must be at least 1.", "error")
            return redirect(url_for("admin_dashboard"))
    elif end_date_raw:
        try:
            end_dt = datetime.strptime(end_date_raw, "%Y-%m-%d").date()
            if end_dt < start_dt:
                flash("End date cannot be earlier than the pickup date.", "error")
                return redirect(url_for("admin_dashboard"))
            rental_days = (end_dt - start_dt).days + 1
        except ValueError:
            flash("Enter a valid end date.", "error")
            return redirect(url_for("admin_dashboard"))

    reservation.pickup_date = pickup_date
    reservation.pickup_time = pickup_time
    if rental_days is not None:
        reservation.days = str(rental_days)
        reservation.nights = str(max(rental_days - 1, 0))
    db.session.commit()
    flash(f"Reservation schedule updated for {reservation.name}.", "success")
    return redirect(request.referrer or url_for("admin_dashboard"))


@app.route("/admin/reservation/<int:res_id>/delete", methods=["POST"])
def delete_reservation(res_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    reservation = db.session.get(Reservation, res_id)
    if reservation is None:
        flash("Reservation not found.", "error")
        return redirect(url_for("admin_dashboard"))

    reservation_name = reservation.name
    van_name = reservation.vans
    db.session.delete(reservation)
    db.session.commit()
    flash(
        f"Deleted {reservation_name}'s reservation for {van_name}.",
        "success",
    )
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/van/<int:van_id>/reservations/delete", methods=["POST"])
def delete_van_reservations(van_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    van = db.session.get(Van, van_id)
    if van is None:
        flash("Van not found.", "error")
        return redirect(url_for("admin_dashboard"))

    if Van.query.filter_by(name=van.name).count() > 1:
        flash(
            "Reservations cannot be safely cleared because multiple vans share "
            "this name. Give each van a unique name first.",
            "error",
        )
        return redirect(url_for("admin_dashboard"))

    deleted_count = Reservation.query.filter_by(vans=van.name).delete(
        synchronize_session=False
    )
    db.session.commit()
    flash(
        f"Deleted {deleted_count} reservation(s) for {van.name}.",
        "success" if deleted_count else "info",
    )
    return redirect(request.referrer or url_for("admin_vans_schedule"))


@app.route("/admin/franchise/approve/<int:app_id>", methods=["POST"])
def approve_franchise(app_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    app_entry = FranchiseApplication.query.get(app_id)
    if app_entry:
        app_entry.status = "Approved"
        app_entry.schedule = request.form.get("schedule")
        db.session.commit()
    return redirect(url_for("admin_dashboard"))




def _find_local_fallback_bytes(filename):
    """Check static/uploads and static/images for legacy files and return (bytes, mimetype) if found."""
    if not filename:
        return None, None
    candidates = [
        os.path.join(app.config["UPLOAD_FOLDER"], filename),
        os.path.join(app.root_path, "static", "images", filename),
    ]
    if filename.startswith("tour_receipt_"):
        stripped = filename[len("tour_receipt_"):]
        candidates.append(os.path.join(app.root_path, "static", "images", stripped))
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, "rb") as fp:
                    data = fp.read()
                mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
                return data, mime
            except Exception:
                pass
    return None, None


def _send_binary_receipt(file_data, stored_mimetype, filename):
    guessed_mimetype = mimetypes.guess_type(filename or "receipt.jpg")[0] or "application/octet-stream"
    safe_inline_types = {"application/pdf", "image/jpeg", "image/png", "image/webp", "image/gif"}
    mimetype = stored_mimetype if stored_mimetype in safe_inline_types else guessed_mimetype
    can_display_inline = mimetype in safe_inline_types
    return send_file(
        BytesIO(file_data),
        mimetype=mimetype,
        as_attachment=not can_display_inline,
        download_name=filename or "receipt",
        max_age=0,
    )


def _render_missing_legacy_receipt_page(record_type, record_id, filename, summary_lines):
    summary_html = "".join(f"<li>{line}</li>" for line in summary_lines)
    upload_url = url_for("admin_upload_receipt", record_type=record_type, record_id=record_id)
    dash_url = url_for("admin_dashboard")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Receipt Viewer - EnAsia Admin</title>
    <style>
        body {{
            font-family: Inter, "Segoe UI", Arial, sans-serif;
            background: #eae5e3;
            color: #231717;
            margin: 0;
            padding: 40px 20px;
            display: flex;
            justify-content: center;
        }}
        .card {{
            background: #faf8f7;
            border: 1px solid #dfd3cf;
            border-top: 4px solid #c9a227;
            border-radius: 14px;
            max-width: 560px;
            width: 100%;
            padding: 28px 30px;
            box-shadow: 0 12px 32px rgba(66, 12, 16, 0.10);
        }}
        .eyebrow {{
            font-size: 11px;
            font-weight: 800;
            letter-spacing: 0.1em;
            text-transform: uppercase;
            color: #9a730c;
            margin: 0 0 6px 0;
        }}
        h2 {{
            margin: 0 0 12px 0;
            color: #420c10;
            font-size: 22px;
        }}
        p {{
            font-size: 14px;
            line-height: 1.55;
            color: #4e3838;
        }}
        ul {{
            background: #f3ecea;
            border: 1px solid #dfd3cf;
            border-radius: 10px;
            padding: 14px 18px 14px 34px;
            margin: 14px 0 18px 0;
            font-size: 13.5px;
            line-height: 1.65;
        }}
        .upload-box {{
            background: linear-gradient(135deg, #f7f0e2 0%, #f5e6e7 100%);
            border: 1px solid rgba(201, 162, 39, 0.55);
            border-radius: 10px;
            padding: 16px 18px;
            margin-top: 16px;
        }}
        .upload-box strong {{
            display: block;
            color: #420c10;
            margin-bottom: 10px;
            font-size: 14px;
        }}
        input[type="file"] {{
            width: 100%;
            margin-bottom: 12px;
            font-size: 13px;
        }}
        .btn {{
            background: linear-gradient(135deg, #420c10 0%, #68181f 100%);
            color: #fffdf9;
            border: 1px solid rgba(212, 175, 55, 0.55);
            padding: 10px 16px;
            border-radius: 7px;
            font-weight: 700;
            font-size: 13.5px;
            cursor: pointer;
            text-decoration: none;
            display: inline-block;
        }}
        .btn-secondary {{
            background: #f6f0ec;
            color: #420c10;
            border: 1px solid #d1bfae;
            margin-left: 8px;
        }}
    </style>
</head>
<body>
    <div class="card">
        <p class="eyebrow">EnAsia NeonDB Receipt Storage</p>
        <h2>🧾 Legacy Receipt Needs Re-Attachment</h2>
        <p>
            The receipt filename <strong>{filename}</strong> was uploaded before <strong>NeonDB binary receipt storage</strong> was enabled, and the temporary container file was cleared during an earlier server redeploy.
        </p>
        <ul>{summary_html}</ul>
        <p>
            All new client and admin receipt uploads are now permanently stored directly inside <strong>NeonDB PostgreSQL (BYTEA)</strong> so they will never be lost on any future redeploy.
        </p>
        <div class="upload-box">
            <strong>📤 Re-attach / Upload Receipt Screenshot to NeonDB Now:</strong>
            <form action="{upload_url}" method="POST" enctype="multipart/form-data">
                <input type="file" name="receipt" accept="image/*,.pdf" required>
                <button type="submit" class="btn">💾 Save Receipt to NeonDB</button>
                <a href="{dash_url}" class="btn btn-secondary">← Back to Dashboard</a>
            </form>
        </div>
    </div>
</body>
</html>""", 200


@app.route("/admin/reservation/<int:res_id>/receipt")
def view_reservation_receipt(res_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    r = db.session.get(Reservation, res_id)
    if r is None or not r.receipt:
        abort(404)
    if r.receipt_data:
        return _send_binary_receipt(r.receipt_data, r.receipt_mimetype, r.receipt)
    data, mime = _find_local_fallback_bytes(r.receipt)
    if data:
        r.receipt_data = data
        r.receipt_mimetype = mime
        db.session.commit()
        return _send_binary_receipt(data, mime, r.receipt)
    return _render_missing_legacy_receipt_page(
        "reservation",
        r.id,
        r.receipt,
        [
            f"<strong>Reservation ID:</strong> #{r.id}",
            f"<strong>Customer:</strong> {r.name} ({r.contact})",
            f"<strong>Van &amp; Destination:</strong> {r.vans} — {r.destination or 'N/A'}",
            f"<strong>Schedule:</strong> {r.pickup_date or 'Not set'} ({r.days} days) · {r.status}",
        ],
    )


@app.route("/admin/tour/<int:app_id>/receipt")
def view_tour_receipt(app_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    t = db.session.get(TourApplication, app_id)
    if t is None or not t.receipt:
        abort(404)
    if t.receipt_data:
        return _send_binary_receipt(t.receipt_data, t.receipt_mimetype, t.receipt)
    data, mime = _find_local_fallback_bytes(t.receipt)
    if data:
        t.receipt_data = data
        t.receipt_mimetype = mime
        db.session.commit()
        return _send_binary_receipt(data, mime, t.receipt)
    return _render_missing_legacy_receipt_page(
        "tour",
        t.id,
        t.receipt,
        [
            f"<strong>Tour Booking ID:</strong> #{t.id}",
            f"<strong>Customer:</strong> {t.full_name} ({t.contact})",
            f"<strong>Tour Package:</strong> {t.tour_package} ({t.tour_date}, {t.pax} pax)",
        ],
    )


@app.route("/admin/gcash/<int:payment_id>/receipt")
def view_gcash_receipt(payment_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    p = db.session.get(GCashPayment, payment_id)
    if p is None or not p.receipt:
        abort(404)
    if p.receipt_data:
        return _send_binary_receipt(p.receipt_data, p.receipt_mimetype, p.receipt)
    data, mime = _find_local_fallback_bytes(p.receipt)
    if data:
        p.receipt_data = data
        p.receipt_mimetype = mime
        db.session.commit()
        return _send_binary_receipt(data, mime, p.receipt)
    return _render_missing_legacy_receipt_page(
        "gcash",
        p.id,
        p.receipt,
        [
            f"<strong>GCash Payment ID:</strong> #{p.id}",
            f"<strong>Sender:</strong> {p.full_name} ({p.contact})",
        ],
    )


@app.route("/admin/receipt/<record_type>/<int:record_id>/upload", methods=["POST"])
def admin_upload_receipt(record_type, record_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    file = request.files.get("receipt")
    if not file or not file.filename:
        flash("Please choose a receipt image or PDF file to upload.", "error")
        return redirect(url_for("admin_dashboard"))

    safe_name = secure_filename(file.filename)
    file_bytes = file.read()
    file_mimetype = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    if not file_bytes:
        flash("Uploaded file is empty.", "error")
        return redirect(url_for("admin_dashboard"))

    filename = f"{record_type}_{record_id}_{uuid.uuid4().hex[:6]}_{safe_name}"[:200]
    try:
        with open(os.path.join(app.config["UPLOAD_FOLDER"], filename), "wb") as fp:
            fp.write(file_bytes)
    except Exception:
        pass

    if record_type == "reservation":
        rec = db.session.get(Reservation, record_id)
        if rec is None:
            abort(404)
        rec.receipt = filename
        rec.receipt_data = file_bytes
        rec.receipt_mimetype = file_mimetype
        db.session.commit()
        flash(f"Receipt saved to NeonDB for reservation #{rec.id} ({rec.name}).", "success")
        return redirect(url_for("view_reservation_receipt", res_id=rec.id))
    elif record_type == "tour":
        rec = db.session.get(TourApplication, record_id)
        if rec is None:
            abort(404)
        rec.receipt = filename
        rec.receipt_data = file_bytes
        rec.receipt_mimetype = file_mimetype
        rec.status = "Receipt Uploaded"
        db.session.commit()
        flash(f"Receipt saved to NeonDB for tour application #{rec.id} ({rec.full_name}).", "success")
        return redirect(url_for("view_tour_receipt", app_id=rec.id))
    elif record_type == "gcash":
        rec = db.session.get(GCashPayment, record_id)
        if rec is None:
            abort(404)
        rec.receipt = filename
        rec.receipt_data = file_bytes
        rec.receipt_mimetype = file_mimetype
        db.session.commit()
        flash(f"Receipt saved to NeonDB for GCash payment #{rec.id} ({rec.full_name}).", "success")
        return redirect(url_for("view_gcash_receipt", payment_id=rec.id))
    abort(404)


@app.route("/static/uploads/<path:filename>")
def serve_uploaded_file_from_neondb(filename):
    """Intercept /static/uploads/<filename> so files stored in NeonDB are served seamlessly even after container restarts."""
    res = Reservation.query.filter_by(receipt=filename).order_by(Reservation.id.desc()).first()
    if res and res.receipt_data:
        return _send_binary_receipt(res.receipt_data, res.receipt_mimetype, res.receipt)

    tour = TourApplication.query.filter_by(receipt=filename).order_by(TourApplication.id.desc()).first()
    if tour and tour.receipt_data:
        return _send_binary_receipt(tour.receipt_data, tour.receipt_mimetype, tour.receipt)

    gcash = GCashPayment.query.filter_by(receipt=filename).order_by(GCashPayment.id.desc()).first()
    if gcash and gcash.receipt_data:
        return _send_binary_receipt(gcash.receipt_data, gcash.receipt_mimetype, gcash.receipt)

    data, mime = _find_local_fallback_bytes(filename)
    if data:
        if res and not res.receipt_data:
            res.receipt_data = data
            res.receipt_mimetype = mime
            db.session.commit()
        elif tour and not tour.receipt_data:
            tour.receipt_data = data
            tour.receipt_mimetype = mime
            db.session.commit()
        elif gcash and not gcash.receipt_data:
            gcash.receipt_data = data
            gcash.receipt_mimetype = mime
            db.session.commit()
        return _send_binary_receipt(data, mime, filename)

    if res and session.get("logged_in"):
        return redirect(url_for("view_reservation_receipt", res_id=res.id))
    if tour and session.get("logged_in"):
        return redirect(url_for("view_tour_receipt", app_id=tour.id))
    if gcash and session.get("logged_in"):
        return redirect(url_for("view_gcash_receipt", payment_id=gcash.id))
    abort(404)


@app.route("/admin/franchise/<int:app_id>/document/<document_type>")
def view_franchise_document(app_id, document_type):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    if document_type not in {"or", "cr"}:
        abort(404)

    application = db.session.get(FranchiseApplication, app_id)
    if application is None:
        abort(404)

    filename = getattr(application, f"{document_type}_file")
    if not filename:
        abort(404)

    # New uploads are read from NeonDB. Older applications are served from
    # the previous local upload folder if that file is still present.
    file_data = getattr(application, f"{document_type}_file_data", None)
    if file_data:
        stored_mimetype = getattr(application, f"{document_type}_file_mimetype", None)
        guessed_mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        safe_inline_types = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
        mimetype = stored_mimetype if stored_mimetype in safe_inline_types else guessed_mimetype
        can_display_inline = mimetype in safe_inline_types
        return send_file(
            BytesIO(file_data),
            mimetype=mimetype,
            as_attachment=not can_display_inline,
            download_name=filename,
            max_age=0,
        )

    # Backward compatibility for records uploaded before this update.
    legacy_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    if os.path.isfile(legacy_path):
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

    abort(404)


@app.route("/admin/franchise/delete/<int:app_id>", methods=["POST"])
def delete_franchise_application(app_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    application = db.session.get(FranchiseApplication, app_id)
    if application is None:
        flash("Franchise application not found.", "error")
        return redirect(url_for("admin_dashboard"))

    applicant_name = application.full_name
    db.session.delete(application)
    db.session.commit()
    flash(f"Deleted the franchise application for {applicant_name}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/tour/delete/<int:app_id>")
def delete_tour(app_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    tour_app = TourApplication.query.get(app_id)
    if tour_app:
        db.session.delete(tour_app)
        db.session.commit()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/vans-schedule")
def admin_vans_schedule():
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    all_reservations = Reservation.query.all()
    all_vans = Van.query.all()
    van_statuses = {
        van.id: get_van_display_status(van, all_reservations)
        for van in all_vans
    }
    return render_template(
        "admin_vans_schedule.html",
        reservations=all_reservations,
        vans=all_vans,
        van_statuses=van_statuses,
    )


@app.route("/admin/van/toggle/<int:van_id>")
def toggle_van_status(van_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    van = Van.query.get(van_id)
    if van:
        if van.status == "Available":
            van.status = "Not Available"
        else:
            van.status = "Available"
        db.session.commit()
    return redirect(request.referrer or url_for("admin_vans_schedule"))


@app.route("/admin/van/<int:van_id>/schedule", methods=["POST"])
def update_van_schedule(van_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    van = db.session.get(Van, van_id)
    if van is None:
        flash("Van not found.", "error")
        return redirect(request.referrer or url_for("admin_vans_schedule"))

    if request.form.get("clear_schedule") == "1":
        van.schedule_date = None
        van.schedule_end_date = None
        van.schedule_days = None
        van.schedule_time = None
        van.schedule_note = None
        db.session.commit()
        flash(f"Cleared manual schedule for {van.name}.", "info")
        return redirect(request.referrer or url_for("admin_vans_schedule"))

    schedule_date = request.form.get("schedule_date", "").strip()
    schedule_end_date = request.form.get("schedule_end_date", "").strip()
    schedule_days_raw = request.form.get("schedule_days", "").strip()
    schedule_time = request.form.get("schedule_time", "").strip()
    schedule_note = request.form.get("schedule_note", "").strip()

    start_dt = None
    end_dt = None
    days_val = None

    try:
        if schedule_date:
            start_dt = datetime.strptime(schedule_date, "%Y-%m-%d").date()
        if schedule_end_date:
            end_dt = datetime.strptime(schedule_end_date, "%Y-%m-%d").date()
        if schedule_time:
            datetime.strptime(schedule_time, "%H:%M")
        if schedule_days_raw:
            days_val = int(schedule_days_raw)
            if days_val < 1:
                raise ValueError
    except ValueError:
        flash("Enter a valid start date, number of days, end date, and time.", "error")
        return redirect(request.referrer or url_for("admin_vans_schedule"))

    if start_dt is not None:
        if end_dt is not None:
            if end_dt < start_dt:
                flash("Reservation end date cannot be earlier than the pick-up / start date.", "error")
                return redirect(request.referrer or url_for("admin_vans_schedule"))
            # If user entered both, keep them consistent:
            computed_days = (end_dt - start_dt).days + 1
            if days_val is not None and days_val != computed_days:
                # Recompute end_dt from days_val if user typed days, unless end_dt was specifically set
                end_dt = start_dt + timedelta(days=days_val - 1)
            else:
                days_val = computed_days
        elif days_val is not None:
            end_dt = start_dt + timedelta(days=days_val - 1)
        else:
            days_val = 1
            end_dt = start_dt
        schedule_end_date = end_dt.isoformat()
    else:
        schedule_end_date = ""
        days_val = None

    van.schedule_date = schedule_date or None
    van.schedule_end_date = schedule_end_date or None
    van.schedule_days = days_val
    van.schedule_time = schedule_time or None
    van.schedule_note = schedule_note or None
    db.session.commit()
    if van.schedule_date:
        flash(
            f"Manual schedule saved for {van.name}: {van.schedule_date} to {van.schedule_end_date} ({van.schedule_days} day{'s' if van.schedule_days != 1 else ''}).",
            "success",
        )
    else:
        flash(f"Manual schedule updated for {van.name}.", "success")
    return redirect(request.referrer or url_for("admin_vans_schedule"))


@app.route("/admin/van/add", methods=["POST"])
def add_van():
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    van_name = request.form.get("van_name")
    plate_number = request.form.get("plate_number")
    driver_name = request.form.get("driver_name")

    if van_name:
        new_van = Van(
            name=van_name,
            plate_number=plate_number,
            driver_name=driver_name,
            status="Available",
        )
        db.session.add(new_van)
        db.session.commit()
        flash(f"Added new van '{van_name}'.", "success")
    return redirect(request.referrer or url_for("admin_vans_schedule"))


@app.route("/admin/van/edit/<int:van_id>", methods=["POST"])
def edit_van(van_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    van = Van.query.get(van_id)
    if van:
        van.name = request.form.get("van_name")
        van.plate_number = request.form.get("plate_number")
        van.driver_name = request.form.get("driver_name")
        db.session.commit()
        flash(f"Updated details for '{van.name}'.", "success")
    return redirect(request.referrer or url_for("admin_vans_schedule"))


@app.route("/admin/rate/add", methods=["POST"])
def add_rate():
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    pickup_location = request.form.get("pickup_location")
    destination = request.form.get("destination")
    rate_per_day = request.form.get("rate_per_day")

    if pickup_location and destination and rate_per_day:
        new_rate = DestinationRate(
            pickup_location=pickup_location,
            destination=destination,
            rate_per_day=float(rate_per_day)
        )
        db.session.add(new_rate)
        db.session.commit()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/rate/edit/<int:rate_id>", methods=["POST"])
def edit_rate(rate_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    rate_entry = DestinationRate.query.get(rate_id)
    if rate_entry:
        rate_entry.pickup_location = request.form.get("pickup_location")
        rate_entry.destination = request.form.get("destination")
        rate_entry.rate_per_day = float(request.form.get("rate_per_day"))
        db.session.commit()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/franchise/add", methods=["POST"])
def add_franchise_option():
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    title = request.form.get("title")
    details = request.form.get("details")

    if title and details:
        new_option = FranchiseOption(title=title, details=details)
        db.session.add(new_option)
        db.session.commit()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/franchise/edit/<int:option_id>", methods=["POST"])
def edit_franchise_option(option_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    option_entry = FranchiseOption.query.get(option_id)
    if option_entry:
        option_entry.title = request.form.get("title")
        option_entry.details = request.form.get("details")
        db.session.commit()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/van-finances")
def admin_van_finances():
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    all_vans = Van.query.order_by(Van.id).all()
    selected_van_id = request.args.get("van_id", type=int)
    month_filter = request.args.get("month", "").strip()

    selected_van = None
    if selected_van_id:
        selected_van = db.session.get(Van, selected_van_id)
    if selected_van is None and all_vans:
        selected_van = all_vans[0]

    all_txs = VanTransaction.query.order_by(VanTransaction.tx_date.desc(), VanTransaction.id.desc()).all()
    available_months = sorted({tx.tx_month for tx in all_txs if tx.tx_month}, reverse=True)
    current_month = datetime.now(BUSINESS_TIMEZONE).strftime("%Y-%m")
    if current_month not in available_months:
        available_months.insert(0, current_month)

    # Compute per-van totals (filtered by month_filter if set, otherwise all time)
    van_summaries = {}
    fleet_earnings = 0.0
    fleet_expenses = 0.0

    for v in all_vans:
        van_summaries[v.id] = {"earnings": 0.0, "expenses": 0.0, "net": 0.0}

    for tx in all_txs:
        if month_filter and tx.tx_month != month_filter:
            continue
        if tx.van_id in van_summaries:
            if tx.tx_type == "earning":
                van_summaries[tx.van_id]["earnings"] += tx.amount
                fleet_earnings += tx.amount
            else:
                van_summaries[tx.van_id]["expenses"] += tx.amount
                fleet_expenses += tx.amount

    for v_id, s in van_summaries.items():
        s["net"] = s["earnings"] - s["expenses"]

    fleet_net = fleet_earnings - fleet_expenses

    # Selected van transactions and monthly breakdown
    selected_txs = []
    monthly_breakdown = []
    van_rentals = []
    recorded_res_ids = {tx.reservation_id for tx in all_txs if tx.reservation_id}

    if selected_van:
        van_all_txs = [tx for tx in all_txs if tx.van_id == selected_van.id]
        selected_txs = [
            tx for tx in van_all_txs
            if (not month_filter or tx.tx_month == month_filter)
        ]

        month_map = {}
        for tx in van_all_txs:
            m_key = tx.tx_month or "Unknown"
            if m_key not in month_map:
                month_map[m_key] = {"month": m_key, "earnings": 0.0, "expenses": 0.0, "net": 0.0}
            if tx.tx_type == "earning":
                month_map[m_key]["earnings"] += tx.amount
            else:
                month_map[m_key]["expenses"] += tx.amount
        for m_key in sorted(month_map.keys(), reverse=True):
            item = month_map[m_key]
            item["net"] = item["earnings"] - item["expenses"]
            monthly_breakdown.append(item)

        # Find reservations for this van and estimate their rental fee
        rates_by_dest = {
            r.destination.strip().lower(): r.rate_per_day
            for r in DestinationRate.query.all()
            if r.destination
        }
        reservations = Reservation.query.filter_by(vans=selected_van.name).order_by(Reservation.id.desc()).all()
        for res in reservations:
            try:
                r_days = max(int(res.days), 2)
            except (TypeError, ValueError):
                r_days = 2
            dest_key = (res.destination or "").strip().lower()
            base_rate = rates_by_dest.get(dest_key, 0.0)
            est_amount = (base_rate + max(r_days - 2, 0) * 3000.0) if base_rate > 0 else 0.0
            van_rentals.append({
                "reservation": res,
                "estimated_amount": est_amount,
                "already_recorded": res.id in recorded_res_ids,
                "default_date": res.pickup_date or datetime.now(BUSINESS_TIMEZONE).strftime("%Y-%m-%d"),
            })

    today_str = datetime.now(BUSINESS_TIMEZONE).strftime("%Y-%m-%d")

    return render_template(
        "admin_van_finances.html",
        vans=all_vans,
        selected_van=selected_van,
        van_summaries=van_summaries,
        fleet_earnings=fleet_earnings,
        fleet_expenses=fleet_expenses,
        fleet_net=fleet_net,
        selected_txs=selected_txs,
        monthly_breakdown=monthly_breakdown,
        van_rentals=van_rentals,
        available_months=available_months,
        month_filter=month_filter,
        today_str=today_str,
    )


@app.route("/admin/van-finances/add-van", methods=["POST"])
def admin_finances_add_van():
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    van_name = request.form.get("van_name", "").strip()
    plate_number = request.form.get("plate_number", "").strip()
    driver_name = request.form.get("driver_name", "").strip()

    if not van_name:
        flash("Van name is required.", "error")
        return redirect(url_for("admin_van_finances"))

    new_van = Van(
        name=van_name,
        plate_number=plate_number or None,
        driver_name=driver_name or None,
        status="Available",
    )
    db.session.add(new_van)
    db.session.commit()
    flash(f"Added new van '{van_name}' ({plate_number or 'No plate'}).", "success")
    return redirect(url_for("admin_van_finances", van_id=new_van.id))


@app.route("/admin/van-finances/edit-van/<int:van_id>", methods=["POST"])
def admin_finances_edit_van(van_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    van = db.session.get(Van, van_id)
    if van is None:
        flash("Van not found.", "error")
        return redirect(url_for("admin_van_finances"))

    old_name = van.name
    new_name = request.form.get("van_name", "").strip() or van.name
    van.name = new_name
    van.plate_number = request.form.get("plate_number", "").strip() or None
    van.driver_name = request.form.get("driver_name", "").strip() or None

    if old_name != new_name:
        for tx in VanTransaction.query.filter_by(van_id=van.id).all():
            tx.van_name = new_name

    db.session.commit()
    flash(f"Updated details for '{van.name}'.", "success")
    return redirect(url_for("admin_van_finances", van_id=van.id))


@app.route("/admin/van-finances/transaction/add", methods=["POST"])
def admin_finances_add_tx():
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    van_id = request.form.get("van_id", type=int)
    van = db.session.get(Van, van_id) if van_id else None
    if van is None:
        flash("Please select a valid van.", "error")
        return redirect(url_for("admin_van_finances"))

    tx_type = request.form.get("tx_type", "earning").strip().lower()
    if tx_type not in ("earning", "expense"):
        tx_type = "earning"

    tx_date = request.form.get("tx_date", "").strip()
    if not tx_date:
        tx_date = datetime.now(BUSINESS_TIMEZONE).strftime("%Y-%m-%d")
    tx_month = tx_date[:7]

    category = request.form.get("category", "").strip() or (
        "Van Rental (Arkila)" if tx_type == "earning" else "Expense"
    )
    description = request.form.get("description", "").strip()
    amount_raw = request.form.get("amount", "0").replace(",", "").replace("₱", "").strip()
    reservation_id = request.form.get("reservation_id", type=int)
    month_filter = request.form.get("month_filter", "").strip()

    try:
        amount = float(amount_raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        flash("Please enter a valid amount greater than 0.", "error")
        return redirect(url_for("admin_van_finances", van_id=van.id, month=month_filter or None))

    new_tx = VanTransaction(
        van_id=van.id,
        van_name=van.name,
        tx_type=tx_type,
        tx_date=tx_date,
        tx_month=tx_month,
        category=category,
        description=description or None,
        amount=amount,
        reservation_id=reservation_id,
    )
    db.session.add(new_tx)
    db.session.commit()

    label = "Earning" if tx_type == "earning" else "Expense"
    flash(f"Recorded {label} of ₱{amount:,.2f} for {van.name}.", "success")
    return redirect(url_for("admin_van_finances", van_id=van.id, month=month_filter or None))


@app.route("/admin/van-finances/transaction/delete/<int:tx_id>", methods=["POST"])
def admin_finances_delete_tx(tx_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))

    tx = db.session.get(VanTransaction, tx_id)
    if tx is None:
        flash("Record not found.", "error")
        return redirect(url_for("admin_van_finances"))

    van_id = tx.van_id
    month_filter = request.form.get("month_filter", "").strip()
    db.session.delete(tx)
    db.session.commit()
    flash("Deleted transaction record.", "success")
    return redirect(url_for("admin_van_finances", van_id=van_id, month=month_filter or None))


@app.route("/admin/logout")
def admin_logout():
    session.pop("logged_in", None)
    session.pop("admin_user_id", None)
    session.pop("admin_username", None)
    session.pop("admin_role", None)
    session.pop("office_db_signed_out", None)
    return redirect(url_for("admin_login"))


@app.route("/health")
def health():
    """Simple health-check endpoint for the hosting platform."""
    return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)# Live build: 2026-11-27-neondb-receipts-and-van-schedule
