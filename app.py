import os
from datetime import date, datetime
from functools import wraps

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

from models import (
    Assignment,
    AuditLog,
    CompletionPhoto,
    ConsentRecord,
    DelayEvent,
    Notification,
    Project,
    Review,
    Task,
    TRADE_TYPES,
    TradeCompany,
    User,
    db,
)
from sms import notify_gc, notify_homeowner, notify_trade

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", os.path.join(BASE_DIR, "uploads"))
ALLOWED_PHOTO_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
SEED_PASSWORD = os.environ.get("SEED_PASSWORD", "password123")

CONSENT_TEXT = (
    "By checking this box, I confirm the homeowner has agreed to receive text "
    "message project updates (milestones and delays) from the general contractor "
    "at the phone number provided. These are informational messages, not "
    "marketing. Message frequency varies. Msg & data rates may apply. Reply "
    "STOP to opt out at any time, HELP for help."
)

STOP_KEYWORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "QUIT", "END"}
START_KEYWORDS = {"START", "UNSTOP", "YES"}

# Plain-language status copy for the homeowner view (no jargon).
PLAIN_STATUS = {
    "pending": "Waiting to be scheduled",
    "invited": "Waiting on trade confirmation",
    "confirmed": "Scheduled — crew confirmed",
    "checked_in": "Crew is on site",
    "complete": "Complete",
    "delayed": "Delayed — new timeline coming soon",
}

STATUS_LABELS = {
    "pending": "Pending",
    "invited": "Invited",
    "confirmed": "Confirmed",
    "checked_in": "Checked In",
    "complete": "Complete",
    "delayed": "Delayed",
    "accepted": "Accepted",
    "declined": "Declined",
}

# Readiness status → (display label, badge class). Reuses the existing
# badge color scheme (green/amber/red) rather than inventing a new one.
READINESS_DISPLAY = {
    "ready": ("Ready", "badge-complete"),
    "at_risk": ("At Risk", "badge-pending"),
    "not_ready": ("Not Ready", "badge-delayed"),
}

DEFAULT_TRADE_COMPANIES = [
    ("Rivera Framing Co.", "framing", "+15555550101", "Metro area"),
    ("Bright Spark Electric", "electrical", "+15555550102", "Metro area"),
    ("Flowright Plumbing", "plumbing", "+15555550103", "Metro area"),
    ("Cool Air HVAC", "HVAC", "+15555550104", "Metro area"),
    ("All-Trade Handyman", "general", "+15555550105", "Metro area"),
]


def create_app():
    app = Flask(__name__)
    db_url = os.environ.get("DATABASE_URL", "sqlite:///trades_scheduling.db")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-secret-key")

    db.init_app(app)

    with app.app_context():
        db.create_all()
        if TradeCompany.query.first() is None:
            for name, trade_type, phone, service_area in DEFAULT_TRADE_COMPANIES:
                db.session.add(
                    TradeCompany(name=name, trade_type=trade_type, phone=phone, service_area=service_area)
                )
            db.session.commit()

        # Seed default users if they don't exist
        if User.query.first() is None:
            # Seed GC
            db.session.add(User(
                username="gc_admin",
                password_hash=generate_password_hash(SEED_PASSWORD),
                role="gc"
            ))
            # Seed Rivera Framing (Subcontractor)
            rivera = TradeCompany.query.filter_by(name="Rivera Framing Co.").first()
            if rivera:
                db.session.add(User(
                    username="rivera_framing",
                    password_hash=generate_password_hash(SEED_PASSWORD),
                    role="trade",
                    trade_company_id=rivera.id
                ))
            # Seed Jamie (Homeowner)
            project = Project.query.filter_by(client_name="Jamie").first()
            if not project:
                project = Project(
                    name="West Remodel",
                    client_name="Jamie",
                    address="456 Oak",
                    client_phone="+15555550100",
                    budget=120000.0
                )
                db.session.add(project)
                db.session.flush()
            
            db.session.add(User(
                username="homeowner_jamie",
                password_hash=generate_password_hash(SEED_PASSWORD),
                role="homeowner",
                project_id=project.id
            ))
            db.session.commit()

    register_routes(app)
    return app


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                flash("Please log in to continue.", "error")
                return redirect(url_for("login"))
            if session.get("role") not in roles:
                flash("You do not have permission to view that page.", "error")
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped

    return decorator


def _allowed_photo(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_PHOTO_EXTENSIONS


def _current_assignment(task):
    """The trade company currently holding this task (most recently accepted)."""
    return (
        Assignment.query.filter_by(task_id=task.id, status="accepted")
        .order_by(Assignment.responded_at.desc())
        .first()
    )


def _log_audit(actor, action, entity_type, entity_id, detail):
    db.session.add(
        AuditLog(actor=actor, action=action, entity_type=entity_type, entity_id=entity_id, detail=detail)
    )


def compute_readiness(task):
    """Whether a trade is actually clear to be dispatched to this task — the
    check that runs before dispatch, not just whether a date is on the calendar.

    Returns {"status": "ready"|"at_risk"|"not_ready", "blockers": [...],
    "warnings": [...], "overridden": bool, "override_reason"/"override_by"}.
    Blockers are hard gates; warnings are shown but don't block check-in.
    """
    blockers = []
    warnings = []

    if task.depends_on_task_id:
        predecessor = Task.query.get(task.depends_on_task_id)
        if predecessor and predecessor.status != "complete":
            blockers.append(f"Previous task ‘{predecessor.name}’ is not complete yet.")

    if not _current_assignment(task):
        blockers.append("No trade has confirmed this task yet.")

    if not task.project.access_instructions:
        warnings.append("No site access or parking instructions on file for this project.")

    if task.readiness_override:
        return {
            "status": "ready",
            "blockers": [],
            "warnings": warnings,
            "overridden": True,
            "override_reason": task.readiness_override_reason,
            "override_by": task.readiness_override_by,
        }

    if blockers:
        status = "not_ready"
    elif warnings:
        status = "at_risk"
    else:
        status = "ready"

    return {"status": status, "blockers": blockers, "warnings": warnings, "overridden": False}


def register_routes(app):
    @app.context_processor
    def inject_globals():
        return {"status_labels": STATUS_LABELS, "readiness_display": READINESS_DISPLAY}

    # ------------------------------------------------------------------
    # Authentication & User Management
    # ------------------------------------------------------------------
    @app.route("/")
    def index():
        if "user_id" in session:
            return redirect(url_for("dashboard_redirect"))

        selected_trade_type = request.args.get("trade_type", "")
        trades_query = TradeCompany.query
        if selected_trade_type in TRADE_TYPES:
            trades_query = trades_query.filter_by(trade_type=selected_trade_type)
        trade_companies = trades_query.order_by(TradeCompany.name).all()

        return render_template(
            "home.html",
            trade_companies=trade_companies,
            trade_types=TRADE_TYPES,
            selected_trade_type=selected_trade_type,
        )

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if "user_id" in session:
            return redirect(url_for("dashboard_redirect"))

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "").strip()

            user = User.query.filter_by(username=username).first()
            if user and check_password_hash(user.password_hash, password):
                session.clear()
                session["user_id"] = user.id
                session["role"] = user.role
                if user.role == "trade":
                    session["trade_company_id"] = user.trade_company_id
                elif user.role == "homeowner":
                    session["project_id"] = user.project_id

                flash("Logged in successfully.", "success")
                return redirect(url_for("dashboard_redirect"))

            flash("Invalid username or password.", "error")
            return redirect(url_for("login"))

        return render_template("login.html")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        # Public self-registration is disabled: this is a single-business
        # internal tool, and letting any visitor pick "role=gc" or pick any
        # existing trade company / homeowner project from a dropdown would let
        # them self-assign access to someone else's data. Trade and homeowner
        # accounts should be provisioned by the GC (a future admin flow); until
        # that exists, use the seeded accounts or create users directly in the
        # database.
        flash("Account creation is managed by your GC administrator — contact them for access.", "error")
        return redirect(url_for("login"))

    @app.route("/logout")
    def logout():
        session.clear()
        flash("Logged out successfully.", "success")
        return redirect(url_for("login"))

    @app.route("/dashboard-redirect")
    def dashboard_redirect():
        if "user_id" not in session:
            return redirect(url_for("login"))
        role = session.get("role")
        if role == "gc":
            return redirect(url_for("gc_dashboard"))
        elif role == "trade":
            return redirect(url_for("trade_queue", trade_company_id=session.get("trade_company_id")))
        elif role == "homeowner":
            return redirect(url_for("homeowner_view", project_id=session.get("project_id")))
        return redirect(url_for("login"))

    @app.route("/account/password", methods=["GET", "POST"])
    @role_required("gc", "trade", "homeowner")
    def change_password():
        if request.method == "POST":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            user = User.query.get_or_404(session["user_id"])

            errors = []
            if not check_password_hash(user.password_hash, current_password):
                errors.append("Current password is incorrect.")
            if len(new_password) < 8:
                errors.append("New password must be at least 8 characters.")
            if new_password != confirm_password:
                errors.append("New password and confirmation do not match.")

            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template("change_password.html")

            user.password_hash = generate_password_hash(new_password)
            _log_audit(user.username, "change_password", "User", user.id, "Changed their own password")
            db.session.commit()
            flash("Password updated.", "success")
            return redirect(url_for("dashboard_redirect"))

        return render_template("change_password.html")

    # ------------------------------------------------------------------
    # GC / PM
    # ------------------------------------------------------------------
    @app.route("/projects")
    @app.route("/dashboard")
    @role_required("gc")
    def gc_dashboard():
        projects = Project.query.order_by(Project.created_at.desc()).all()
        today = date.today()

        project_rows = []
        for project in projects:
            tasks = project.tasks
            late_tasks = [
                t for t in tasks if t.scheduled_date and t.scheduled_date < today and t.status != "complete"
            ]
            pending_invites = [a for t in tasks for a in t.assignments if a.status == "invited"]
            needs_trade = [t for t in tasks if t.status == "pending"]
            upcoming = [
                t
                for t in tasks
                if t.status in ("confirmed", "checked_in")
                and t.scheduled_date
                and t.scheduled_date >= today
            ]
            project_rows.append(
                {
                    "project": project,
                    "late_tasks": late_tasks,
                    "pending_invites": pending_invites,
                    "needs_trade": needs_trade,
                    "upcoming": upcoming,
                }
            )

        return render_template("gc_dashboard.html", project_rows=project_rows, today=today)

    @app.route("/projects/new", methods=["GET", "POST"])
    @role_required("gc")
    def new_project():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            client_name = request.form.get("client_name", "").strip()
            client_phone = request.form.get("client_phone", "").strip()
            client_email = request.form.get("client_email", "").strip() or None
            address = request.form.get("address", "").strip()
            consent = request.form.get("consent")
            access_instructions = request.form.get("access_instructions", "").strip() or None

            budget_str = request.form.get("budget", "0").strip()
            try:
                budget = float(budget_str)
            except ValueError:
                budget = 0.0

            errors = []
            if not name:
                errors.append("Project name is required.")
            if not client_name:
                errors.append("Client name is required.")
            if not client_phone:
                errors.append("Client phone is required.")
            if not address:
                errors.append("Address is required.")

            if errors:
                for error in errors:
                    flash(error, "error")
                return (
                    render_template("new_project.html", form=request.form, consent_text=CONSENT_TEXT),
                    400,
                )

            project = Project(
                name=name,
                address=address,
                client_name=client_name,
                client_phone=client_phone or None,
                client_email=client_email,
                budget=budget,
                access_instructions=access_instructions,
            )
            db.session.add(project)
            db.session.flush()

            if consent:
                db.session.add(
                    ConsentRecord(
                        project_id=project.id,
                        phone=client_phone,
                        consent_text=CONSENT_TEXT,
                        ip_address=request.remote_addr,
                    )
                )

            _log_audit(
                "GC/PM", "create_project", "Project", project.id, f"Created project '{name}' for {client_name}"
            )
            db.session.commit()
            flash("Project created.", "success")
            return redirect(url_for("project_detail", project_id=project.id))

        return render_template("new_project.html", form={}, consent_text=CONSENT_TEXT)

    @app.route("/projects/<int:project_id>")
    @role_required("gc")
    def project_detail(project_id):
        project = Project.query.get_or_404(project_id)
        tasks = Task.query.filter_by(project_id=project_id).order_by(Task.sequence_order).all()
        trade_companies = TradeCompany.query.order_by(TradeCompany.name).all()
        readiness_by_task = {t.id: compute_readiness(t) for t in tasks}
        return render_template(
            "project_detail.html",
            project=project,
            tasks=tasks,
            trade_companies=trade_companies,
            trade_types=TRADE_TYPES,
            readiness_by_task=readiness_by_task,
        )

    @app.route("/projects/<int:project_id>/access-instructions", methods=["POST"])
    @role_required("gc")
    def update_access_instructions(project_id):
        project = Project.query.get_or_404(project_id)
        project.access_instructions = request.form.get("access_instructions", "").strip() or None
        _log_audit(
            "GC/PM", "update_access_instructions", "Project", project.id, "Updated site access instructions"
        )
        db.session.commit()
        flash("Access instructions updated.", "success")
        return redirect(url_for("project_detail", project_id=project_id))

    @app.route("/tasks/<int:task_id>/invite", methods=["POST"])
    @role_required("gc")
    def invite_trade(task_id):
        task = Task.query.get_or_404(task_id)
        trade_company_id = request.form.get("trade_company_id")
        if not trade_company_id:
            flash("Choose a trade company to invite.", "error")
            return redirect(url_for("project_detail", project_id=task.project_id))

        trade_company = TradeCompany.query.get_or_404(int(trade_company_id))

        assignment = Assignment(task_id=task.id, trade_company_id=trade_company.id, status="invited")
        db.session.add(assignment)
        task.status = "invited"

        notify_trade(
            trade_company,
            "task_invited",
            f"You're invited to {task.name} on {task.project.name} ({task.project.address}).",
        )
        _log_audit(
            "GC/PM", "invite_trade", "Task", task.id, f"Invited {trade_company.name} to task '{task.name}'"
        )
        db.session.commit()
        flash(f"{trade_company.name} invited.", "success")
        return redirect(url_for("project_detail", project_id=task.project_id))

    @app.route("/audit")
    @role_required("gc")
    def audit_log():
        entries = AuditLog.query.order_by(AuditLog.timestamp.desc()).all()
        return render_template("audit.html", entries=entries)

    # ------------------------------------------------------------------
    # Trade
    # ------------------------------------------------------------------
    @app.route("/trade/<int:trade_company_id>")
    @role_required("trade")
    def trade_queue(trade_company_id):
        if session.get("trade_company_id") != trade_company_id:
            flash("That's not your company.", "error")
            return redirect(url_for("login"))

        trade_company = TradeCompany.query.get_or_404(trade_company_id)

        pending_invitations = (
            Assignment.query.filter_by(trade_company_id=trade_company_id, status="invited")
            .order_by(Assignment.invited_at)
            .all()
        )
        active_assignments = (
            Assignment.query.filter_by(trade_company_id=trade_company_id, status="accepted")
            .join(Task)
            .filter(Task.status.in_(["confirmed", "checked_in", "delayed"]))
            .all()
        )

        return render_template(
            "trade_queue.html",
            trade_company=trade_company,
            pending_invitations=pending_invitations,
            active_assignments=active_assignments,
        )

    @app.route("/assignments/<int:assignment_id>/accept", methods=["POST"])
    @role_required("trade")
    def accept_assignment(assignment_id):
        assignment = Assignment.query.get_or_404(assignment_id)
        if session.get("trade_company_id") != assignment.trade_company_id:
            flash("That invitation doesn't belong to your company.", "error")
            return redirect(url_for("login"))

        assignment.status = "accepted"
        assignment.responded_at = datetime.utcnow()
        task = assignment.task
        task.status = "confirmed"

        notify_gc(
            "assignment_accepted",
            f"{assignment.trade_company.name} accepted {task.name} on {task.project.name}.",
        )
        _log_audit(
            assignment.trade_company.name,
            "accept_assignment",
            "Assignment",
            assignment.id,
            f"Accepted task '{task.name}' on project '{task.project.name}'",
        )
        db.session.commit()
        flash("Invitation accepted.", "success")
        return redirect(url_for("trade_queue", trade_company_id=assignment.trade_company_id))

    @app.route("/assignments/<int:assignment_id>/decline", methods=["POST"])
    @role_required("trade")
    def decline_assignment(assignment_id):
        assignment = Assignment.query.get_or_404(assignment_id)
        if session.get("trade_company_id") != assignment.trade_company_id:
            flash("That invitation doesn't belong to your company.", "error")
            return redirect(url_for("login"))

        assignment.status = "declined"
        assignment.responded_at = datetime.utcnow()
        task = assignment.task
        task.status = "pending"  # needs a new trade invited

        notify_gc(
            "assignment_declined",
            f"{assignment.trade_company.name} declined {task.name} on {task.project.name} — needs a new invite.",
        )
        _log_audit(
            assignment.trade_company.name,
            "decline_assignment",
            "Assignment",
            assignment.id,
            f"Declined task '{task.name}' on project '{task.project.name}'",
        )
        db.session.commit()
        flash("Invitation declined.", "success")
        return redirect(url_for("trade_queue", trade_company_id=assignment.trade_company_id))

    @app.route("/tasks/<int:task_id>")
    def task_detail(task_id):
        task = Task.query.get_or_404(task_id)
        role = session.get("role")
        assignment = _current_assignment(task)

        is_gc = role == "gc"
        is_assigned_trade = (
            role == "trade"
            and assignment is not None
            and session.get("trade_company_id") == assignment.trade_company_id
        )
        if not (is_gc or is_assigned_trade):
            flash("You don't have access to that task.", "error")
            return redirect(url_for("login"))

        return render_template(
            "task_detail.html",
            task=task,
            assignment=assignment,
            can_act=is_assigned_trade,
            is_gc=is_gc,
            role=role,
            readiness=compute_readiness(task),
        )

    @app.route("/tasks/<int:task_id>/checkin", methods=["POST"])
    @role_required("trade")
    def checkin_task(task_id):
        task = Task.query.get_or_404(task_id)
        assignment = _current_assignment(task)
        if not assignment or session.get("trade_company_id") != assignment.trade_company_id:
            flash("You don't have access to that task.", "error")
            return redirect(url_for("login"))

        readiness = compute_readiness(task)
        if readiness["status"] == "not_ready":
            flash(
                "This job isn't ready yet: " + " ".join(readiness["blockers"])
                + " Ask your GC to resolve this or override the readiness check.",
                "error",
            )
            return redirect(url_for("task_detail", task_id=task.id))

        task.status = "checked_in"
        _log_audit(
            assignment.trade_company.name, "check_in", "Task", task.id, f"Checked in to task '{task.name}'"
        )
        db.session.commit()
        flash("Checked in.", "success")
        return redirect(url_for("task_detail", task_id=task.id))

    @app.route("/tasks/<int:task_id>/override-readiness", methods=["POST"])
    @role_required("gc")
    def override_readiness(task_id):
        task = Task.query.get_or_404(task_id)
        reason = request.form.get("reason", "").strip()
        if not reason:
            flash("Please explain why you're overriding the readiness check.", "error")
            return redirect(url_for("task_detail", task_id=task_id))

        gc_user = User.query.get(session.get("user_id"))
        task.readiness_override = True
        task.readiness_override_reason = reason
        task.readiness_override_by = gc_user.username if gc_user else "GC/PM"

        _log_audit(
            task.readiness_override_by,
            "override_readiness",
            "Task",
            task.id,
            f"Overrode readiness check for '{task.name}': {reason}",
        )
        db.session.commit()
        flash("Readiness override applied — the trade can now check in.", "success")
        return redirect(url_for("task_detail", task_id=task_id))

    @app.route("/tasks/<int:task_id>/complete", methods=["POST"])
    @role_required("trade")
    def complete_task(task_id):
        task = Task.query.get_or_404(task_id)
        assignment = _current_assignment(task)
        if not assignment or session.get("trade_company_id") != assignment.trade_company_id:
            flash("You don't have access to that task.", "error")
            return redirect(url_for("login"))

        photo = request.files.get("photo")
        caption = request.form.get("caption", "").strip() or None
        has_existing_photo = CompletionPhoto.query.filter_by(task_id=task.id).first() is not None

        if (not photo or not photo.filename) and not has_existing_photo:
            flash("A completion photo is required to mark this task complete.", "error")
            return redirect(url_for("task_detail", task_id=task.id))

        if photo and photo.filename:
            if not _allowed_photo(photo.filename):
                flash("Unsupported photo file type.", "error")
                return redirect(url_for("task_detail", task_id=task.id))
            os.makedirs(UPLOAD_FOLDER, exist_ok=True)
            filename = secure_filename(f"{task.id}_{int(datetime.utcnow().timestamp())}_{photo.filename}")
            photo.save(os.path.join(UPLOAD_FOLDER, filename))
            db.session.add(CompletionPhoto(task_id=task.id, filename=filename, caption=caption))

        task.status = "complete"
        _log_audit(
            assignment.trade_company.name, "complete_task", "Task", task.id, f"Marked task '{task.name}' complete"
        )

        # Auto-notify whoever's already assigned to the next task in sequence.
        next_task = (
            Task.query.filter(Task.project_id == task.project_id, Task.sequence_order > task.sequence_order)
            .order_by(Task.sequence_order)
            .first()
        )
        if next_task:
            next_assignment = (
                Assignment.query.filter(
                    Assignment.task_id == next_task.id, Assignment.status.in_(["invited", "accepted"])
                )
                .order_by(Assignment.invited_at.desc())
                .first()
            )
            if next_assignment:
                notify_trade(
                    next_assignment.trade_company,
                    "clear_to_start",
                    f"{task.name} is complete on {task.project.name} — you're clear to start {next_task.name}.",
                )
            else:
                notify_gc(
                    "next_task_needs_trade",
                    f"{next_task.name} on {task.project.name} needs a trade invited now that {task.name} is complete.",
                )

        homeowner_message = f"Good news — {task.name} is complete on your project."
        if next_task:
            homeowner_message += f" {next_task.trade_type_needed.capitalize()} work is up next."
        else:
            homeowner_message += " That was the last scheduled task on your project."
        notify_homeowner(task.project, "task_complete", homeowner_message)

        db.session.commit()
        flash("Task marked complete.", "success")
        return redirect(url_for("task_detail", task_id=task.id))

    @app.route("/tasks/<int:task_id>/delay", methods=["POST"])
    @role_required("trade")
    def report_delay(task_id):
        task = Task.query.get_or_404(task_id)
        assignment = _current_assignment(task)
        if not assignment or session.get("trade_company_id") != assignment.trade_company_id:
            flash("You don't have access to that task.", "error")
            return redirect(url_for("login"))

        reason = request.form.get("reason", "").strip()
        if not reason:
            flash("Please describe the reason for the delay.", "error")
            return redirect(url_for("task_detail", task_id=task.id))

        db.session.add(DelayEvent(task_id=task.id, reason=reason))
        task.status = "delayed"
        _log_audit(
            assignment.trade_company.name,
            "report_delay",
            "Task",
            task.id,
            f"Reported delay on '{task.name}': {reason}",
        )

        notify_gc("task_delayed", f"{task.name} on {task.project.name} delayed: {reason}")
        notify_homeowner(
            task.project,
            "task_delayed",
            f"Quick update — {task.name} on your project is running behind schedule. "
            f"We'll follow up with a new timeline soon.",
        )

        db.session.commit()
        flash("Delay reported.", "success")
        return redirect(url_for("task_detail", task_id=task.id))

    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename):
        return send_from_directory(UPLOAD_FOLDER, filename)

    # ------------------------------------------------------------------
    # Homeowner
    # ------------------------------------------------------------------
    @app.route("/homeowner/<int:project_id>")
    @role_required("homeowner")
    def homeowner_view(project_id):
        if session.get("project_id") != project_id:
            flash("That's not your project.", "error")
            return redirect(url_for("login"))

        project = Project.query.get_or_404(project_id)
        tasks = Task.query.filter_by(project_id=project_id).order_by(Task.sequence_order).all()
        active_delays = (
            DelayEvent.query.join(Task)
            .filter(Task.project_id == project_id, Task.status == "delayed")
            .order_by(DelayEvent.reported_at.desc())
            .all()
        )
        next_up = next((t for t in tasks if t.status != "complete"), None)

        reviewable_trade_ids = (
            db.session.query(Assignment.trade_company_id)
            .join(Task)
            .filter(Task.project_id == project_id, Task.status == "complete", Assignment.status == "accepted")
            .distinct()
        )
        reviewable_trades = TradeCompany.query.filter(TradeCompany.id.in_(reviewable_trade_ids)).all()
        existing_reviews = {
            r.trade_company_id: r for r in Review.query.filter_by(project_id=project_id).all()
        }

        return render_template(
            "homeowner_view.html",
            project=project,
            tasks=tasks,
            active_delays=active_delays,
            next_up=next_up,
            plain_status=PLAIN_STATUS,
            reviewable_trades=reviewable_trades,
            existing_reviews=existing_reviews,
        )

    @app.route("/projects/<int:project_id>/trades/<int:trade_company_id>/review", methods=["POST"])
    @role_required("homeowner")
    def submit_review(project_id, trade_company_id):
        if session.get("project_id") != project_id:
            flash("That's not your project.", "error")
            return redirect(url_for("login"))

        is_reviewable = (
            db.session.query(Assignment.id)
            .join(Task)
            .filter(
                Task.project_id == project_id,
                Task.status == "complete",
                Assignment.status == "accepted",
                Assignment.trade_company_id == trade_company_id,
            )
            .first()
            is not None
        )
        if not is_reviewable:
            flash("You can only review a trade that has completed work on this project.", "error")
            return redirect(url_for("homeowner_view", project_id=project_id))

        try:
            rating = int(request.form.get("rating", ""))
        except ValueError:
            rating = 0
        comment = request.form.get("comment", "").strip() or None

        if rating < 1 or rating > 5:
            flash("Rating must be between 1 and 5 stars.", "error")
            return redirect(url_for("homeowner_view", project_id=project_id))

        review = Review.query.filter_by(project_id=project_id, trade_company_id=trade_company_id).first()
        trade_company = TradeCompany.query.get_or_404(trade_company_id)
        if review:
            review.rating = rating
            review.comment = comment
            action = "update_review"
        else:
            review = Review(project_id=project_id, trade_company_id=trade_company_id, rating=rating, comment=comment)
            db.session.add(review)
            action = "leave_review"

        _log_audit(
            "Homeowner", action, "Review", trade_company_id, f"{rating}-star review for {trade_company.name}"
        )
        db.session.commit()
        flash("Review saved. Thank you!", "success")
        return redirect(url_for("homeowner_view", project_id=project_id))

    # ------------------------------------------------------------------
    # Inbound SMS (STOP/START opt-out handling — unchanged logic, now
    # matched by phone across ConsentRecords tied to projects).
    # ------------------------------------------------------------------
    @app.route("/sms/inbound", methods=["POST"])
    def sms_inbound():
        from_number = request.form.get("From", "").strip()
        body = request.form.get("Body", "").strip().upper()

        if body in STOP_KEYWORDS or body in START_KEYWORDS:
            opted_out = body in STOP_KEYWORDS
            now = datetime.utcnow()
            for record in ConsentRecord.query.filter_by(phone=from_number).all():
                record.opted_out = opted_out
                record.opted_out_at = now if opted_out else None
            db.session.commit()
        # HELP, or any unrecognized keyword: no state change.

        return Response("<Response></Response>", mimetype="text/xml")

    @app.route("/blueprint")
    def blueprint_view():
        return render_template("blueprint.html")

    @app.route("/projects/<int:project_id>/tasks/new", methods=["GET", "POST"])
    @role_required("gc")
    def new_task(project_id):
        project = Project.query.get_or_404(project_id)
        existing_tasks = Task.query.filter_by(project_id=project_id).order_by(Task.sequence_order).all()

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            trade_type_needed = request.form.get("trade_type_needed", "")
            insert_after_id = request.form.get("insert_after") or None

            if not name or trade_type_needed not in TRADE_TYPES:
                flash("Please provide a task name and a valid trade type.", "error")
                return render_template(
                    "new_task.html", project=project, trade_types=TRADE_TYPES, existing_tasks=existing_tasks
                )

            if insert_after_id:
                after_task = Task.query.get_or_404(int(insert_after_id))
                new_order = after_task.sequence_order + 1
                depends_on = after_task.id
            else:
                new_order = 1
                depends_on = None

            for t in existing_tasks:
                if t.sequence_order >= new_order:
                    t.sequence_order += 1

            task = Task(
                project_id=project_id,
                trade_type_needed=trade_type_needed,
                name=name,
                sequence_order=new_order,
                depends_on_task_id=depends_on,
                status="pending",
            )
            db.session.add(task)
            db.session.flush()

            _log_audit(
                "GC/PM",
                "add_task",
                "Task",
                task.id,
                f"Added task '{name}' ({trade_type_needed}) to project {project.name}",
            )
            db.session.commit()
            flash("Task added.", "success")
            return redirect(url_for("project_detail", project_id=project_id))

        return render_template(
            "new_task.html", project=project, trade_types=TRADE_TYPES, existing_tasks=existing_tasks
        )


app = create_app()

if __name__ == "__main__":
    app.run(port=5001, debug=True, use_reloader=False)
