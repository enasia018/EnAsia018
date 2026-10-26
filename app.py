import os
import smtplib
from email.message import EmailMessage
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
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

db = SQLAlchemy(app)






UPLOAD_FOLDER = "static/uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


class Reservation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    address = db.Column(db.String(200), nullable=False)
    contact = db.Column(db.String(50), nullable=False)
    vans = db.Column(db.String(50), nullable=False)
    pickup = db.Column(db.String(100), nullable=False)
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
    or_file = db.Column(db.String(200), nullable=False)
    cr_file = db.Column(db.String(200), nullable=False)
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


with app.app_context():
    db.create_all()
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

        or_file = request.files.get("or_file")
        or_filename = ""
        if or_file and or_file.filename != "":
            or_filename = secure_filename(f"or{option_id}_" + or_file.filename)
            or_file.save(os.path.join(app.config["UPLOAD_FOLDER"], or_filename))

        cr_file = request.files.get("cr_file")
        cr_filename = ""
        if cr_file and cr_file.filename != "":
            cr_filename = secure_filename(f"cr{option_id}_" + cr_file.filename)
            cr_file.save(os.path.join(app.config["UPLOAD_FOLDER"], cr_filename))

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
            status=f"Pending (Option {option_id})"
        )
        db.session.add(new_app)
        db.session.commit()

        return render_template("franchise_apply.html", success=True, option_id=option_id)

    return render_template("franchise_apply.html", success=False, option_id=option_id)


@app.route("/vans-list")
def vans_list():
    all_vans = Van.query.all()
    return render_template("vans_list.html", vans=all_vans)


@app.route("/tour-package")
def tour_package():
    return render_template("tour_package.html")


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

    if request.method == "POST":
        name = request.form.get("name")
        address = request.form.get("address")
        contact = request.form.get("contact")
        vans = request.form.get("vans")
        pickup = request.form.get("pickup")
        dropoff = request.form.get("dropoff")
        days = request.form.get("days")
        nights = request.form.get("nights")

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
            dropoff=dropoff,
            days=days,
            nights=nights,
            receipt=filename,
            status="Pending",
        )
        db.session.add(new_reservation)
        db.session.commit()

        return render_template(
            "rental.html", success=True, vans_list=available_vans, rates=all_rates
        )

    return render_template(
        "rental.html", success=False, vans_list=available_vans, rates=all_rates
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
    return render_template(
        "admin_dashboard.html",
        reservations=all_reservations,
        vans=all_vans,
        rates=all_rates,
        franchise_options=all_options,
        franchise_apps=all_franchise_apps,
        tour_apps=all_tour_apps,
        gcash_payments=all_gcash_payments,
    )


@app.route("/admin/approve/<int:res_id>")
def approve_reservation(res_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    reservation = Reservation.query.get(res_id)
    if reservation:
        reservation.status = "Approved"
        db.session.commit()
    return redirect(url_for("admin_dashboard"))


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


@app.route("/admin/tour/delete/<int:app_id>")
def delete_tour(app_id):
    if not session.get("logged_in"):
        return redirect(url_for("admin_login"))
    tour_app = TourApplication.query.get(app_id)
    if tour_app:
        db.session.delete(tour_app)
        db.session.commit()
    return redirect(url_for("admin_dashboard"))


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
    return redirect(url_for("admin_dashboard"))


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
    return redirect(url_for("admin_dashboard"))


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
    return redirect(url_for("admin_dashboard"))


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


@app.route("/admin/logout")
def admin_logout():
    session.pop("logged_in", None)
    return redirect(url_for("admin_login"))


if __name__ == "__main__":
    app.run(debug=True)