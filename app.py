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

db = SQLAlchemy(app)

BUSINESS_TIMEZONE = ZoneInfo("Asia/Manila")


def reservation_period(reservation):
    try:
        start_date = datetime.strptime(reservation.pickup_date, "%Y-%m-%d").date()
        days = max(int(reservation.days), 1)
    except (TypeError, ValueError):
        return None
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
    return None


def get_van_display_status(van, reservations, today=None):
    if van.status != "Available":
        return "Not Available"

    today = today or datetime.now(BUSINESS_TIMEZONE).date()
    reserved_through = today + timedelta(days=3)
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
    status = db.Column(db.String(50), default="Pending")


class Van(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    plate_number = db.Column(db.String(50), nullable=True)
    driver_name = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(50), default="Available")
    schedule_date = db.Column(db.String(10), nullable=True)
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
    status = db.Column(db.String(50), default="Pending")


class GCashPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(100), nullable=False)
    contact = db.Column(db.String(50), nullable=False)
    receipt = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(50), default="Pending")


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

    van_columns = {column["name"] for column in inspect(db.engine).get_columns("van")}
    with db.engine.begin() as connection:
        if "schedule_date" not in van_columns:
            connection.execute(text("ALTER TABLE van ADD COLUMN schedule_date VARCHAR(10)"))
        if "schedule_time" not in van_columns:
            connection.execute(text("ALTER TABLE van ADD COLUMN schedule_time VARCHAR(5)"))
        if "schedule_note" not in van_columns:
            connection.execute(text("ALTER TABLE van ADD COLUMN schedule_note VARCHAR(500)"))

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
                )
                m.set_password("enasia2026")
                db.session.add(m)
            db.session.commit()

ADMIN_USER = "admin"
ADMIN_PASS = "enasia2026"


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
        filename = secure_filename(f"tour_receipt_" + file.filename)
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

        tour_app = TourApplication.query.filter(
            db.func.lower(TourApplication.full_name) == sender_name
        ).first()

        if tour_app:
            tour_app.receipt = filename
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
        if file and file.filename != "":
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

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
        filename = secure_filename(f"gcash_{file.filename}")
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

        new_payment = GCashPayment(
            full_name=full_name,
            contact=contact,
            receipt=filename,
            status="Pending"
        )
        db.session.add(new_payment)
        db.session.commit()

    return render_template("gcash.html", success=True)


@app.route("/upload_receipt/<int:res_id>", methods=["POST"])
def upload_receipt(res_id):
    file = request.files.get("receipt")
    if file and file.filename != "":
        filename = secure_filename(file.filename)
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

        reservation = Reservation.query.get(res_id)
        if reservation:
            reservation.receipt = filename
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
            member = found
        else:
            error = "Invalid Member ID or password. Please try again."

    return render_template("members.html", member=member, error=error)


@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if (
            request.form.get("username") == ADMIN_USER
            and request.form.get("password") == ADMIN_PASS
        ):
            session["logged_in"] = True
            return redirect(url_for("admin_dashboard"))
        else:
            error = "Invalid username or password!"
    return render_template("admin_login.html", error=error)


@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    all_reservations = Reservation.query.all()
    all_vans = Van.query.all()
    all_rates = DestinationRate.query.all()
    all_options = FranchiseOption.query.all()
    all_franchise_apps = FranchiseApplication.query.all()
    all_tour_apps = TourApplication.query.all()
    all_gcash_payments = GCashPayment.query.all()
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
    )


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
    )
    new_member.set_password(password)
    db.session.add(new_member)
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
    db.session.commit()
    flash(
        f"Password for {member.full_name} ({member.member_code}) has been reset.",
        "success",
    )
    return redirect(url_for("admin_members_page", selected_id=member.id))


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
    default_pw = request.form.get("default_password", "enasia2026").strip() or "enasia2026"
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
            target = Member(member_code=code, full_name=full_name)
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
    try:
        datetime.strptime(pickup_date, "%Y-%m-%d")
        datetime.strptime(pickup_time, "%H:%M")
    except ValueError:
        flash("Enter a valid pickup date and time.", "error")
        return redirect(url_for("admin_dashboard"))

    reservation = db.session.get(Reservation, res_id)
    if reservation is None:
        flash("Reservation not found.", "error")
        return redirect(url_for("admin_dashboard"))

    reservation.pickup_date = pickup_date
    reservation.pickup_time = pickup_time
    db.session.commit()
    flash(f"Pickup schedule updated for {reservation.name}.", "success")
    return redirect(url_for("admin_dashboard"))


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

    schedule_date = request.form.get("schedule_date", "").strip()
    schedule_time = request.form.get("schedule_time", "").strip()
    schedule_note = request.form.get("schedule_note", "").strip()

    try:
        if schedule_date:
            datetime.strptime(schedule_date, "%Y-%m-%d")
        if schedule_time:
            datetime.strptime(schedule_time, "%H:%M")
    except ValueError:
        flash("Enter a valid manual schedule date and time.", "error")
        return redirect(request.referrer or url_for("admin_vans_schedule"))

    van = db.session.get(Van, van_id)
    if van is None:
        flash("Van not found.", "error")
        return redirect(request.referrer or url_for("admin_vans_schedule"))

    van.schedule_date = schedule_date or None
    van.schedule_time = schedule_time or None
    van.schedule_note = schedule_note or None
    db.session.commit()
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
    return redirect(url_for("admin_login"))


@app.route("/health")
def health():
    """Simple health-check endpoint for the hosting platform."""
    return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)