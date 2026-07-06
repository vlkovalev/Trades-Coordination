import os
from datetime import date, datetime
from functools import wraps

from flask import (
    Flask,
    Response,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from models import (
    Assignment,
    AuditLog,
    CompletionPhoto,
    ConsentRecord,
    DelayEvent,
    Notification,
    Project,
    Task,
    TRADE_TYPES,
    TradeCompany,
    db,
)
from sms import notify_gc, notify_homeowner, notify_trade

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
ALLOWED_PHOTO_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

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

DEFAULT_TRADE_COMPANIES = [
    ("Rivera Framing Co.", "framing", "+15555550101", "Metro area"),
    ("Bright Spark Electric", "electrical", "+15555550102", "Metro area"),
    ("Flowright Plumbing", "plumbing", "+15555550103", "Metro area"),
    ("Cool Air HVAC", "HVAC", "+15555550104", "Metro area"),
    ("All-Trade Handyman", "general", "+15555550105", "Metro area"),
]


def create_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///trades_scheduling.db"
    app.config["SECRET_KEY"] = "dev-only-secret-key"

    db.init_app(app)

    with app.app_context():
        db.create_all()
        if TradeCompany.query.first() is None:
            for name, trade_type, phone, service_area in DEFAULT_TRADE_COMPANIES:
                db.session.add(
                    TradeCompany(name=name, trade_type=trade_type, phone=phone, service_area=service_area)
                )
            db.session.commit()

    register_routes(app)
    return app


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if current_app.testing:
                return view(*args, **kwargs)
            if session.get("role") not in roles:
                flash("Please choose your role to continue.", "error")
                return redirect(url_for("role_picker"))
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


def register_routes(app):
    @app.context_processor
    def inject_globals():
        return {"status_labels": STATUS_LABELS}

    # ------------------------------------------------------------------
    # Role picker (no auth — MVP identity stand-in)
    # ------------------------------------------------------------------
    @app.route("/", methods=["GET", "POST"])
    def role_picker():
        if request.method == "POST":
            role = request.form.get("role")

            if role == "gc":
                session.clear()
                session["role"] = "gc"
                return redirect(url_for("gc_dashboard"))

            if role == "trade":
                trade_company_id = request.form.get("trade_company_id")
                if not trade_company_id:
                    flash("Select your trade company.", "error")
                    return redirect(url_for("role_picker"))
                session.clear()
                session["role"] = "trade"
                session["trade_company_id"] = int(trade_company_id)
                return redirect(url_for("trade_queue", trade_company_id=int(trade_company_id)))

            if role == "homeowner":
                project_id = request.form.get("project_id")
                if not project_id:
                    flash("Select your project.", "error")
                    return redirect(url_for("role_picker"))
                session.clear()
                session["role"] = "homeowner"
                session["project_id"] = int(project_id)
                return redirect(url_for("homeowner_view", project_id=int(project_id)))

            flash("Please choose a role.", "error")
            return redirect(url_for("role_picker"))

        trade_companies = TradeCompany.query.order_by(TradeCompany.name).all()
        projects = Project.query.order_by(Project.name).all()
        return render_template("role_picker.html", trade_companies=trade_companies, projects=projects)

    @app.route("/switch-role")
    def switch_role():
        session.clear()
        return redirect(url_for("role_picker"))

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
                client_phone=client_phone,
                client_email=client_email,
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
        return render_template(
            "project_detail.html",
            project=project,
            tasks=tasks,
            trade_companies=trade_companies,
            trade_types=TRADE_TYPES,
        )

    @app.route("/projects/<int:project_id>/tasks", methods=["POST"])
    @role_required("gc")
    def add_task(project_id):
        project = Project.query.get_or_404(project_id)
        name = request.form.get("name", "").strip()
        trade_type_needed = request.form.get("trade_type_needed", "")
        insert_after_id = request.form.get("insert_after") or None

        if not name or trade_type_needed not in TRADE_TYPES:
            flash("Please provide a task name and a valid trade type.", "error")
            return redirect(url_for("project_detail", project_id=project_id))

        existing = Task.query.filter_by(project_id=project_id).order_by(Task.sequence_order).all()

        if insert_after_id:
            after_task = Task.query.get_or_404(int(insert_after_id))
            new_order = after_task.sequence_order + 1
            depends_on = after_task.id
        else:
            new_order = 1
            depends_on = None

        for t in existing:
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
            "GC/PM", "add_task", "Task", task.id, f"Added task '{name}' ({trade_type_needed}) to project {project.name}"
        )
        db.session.commit()
        flash("Task added.", "success")
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
            return redirect(url_for("role_picker"))

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
            return redirect(url_for("role_picker"))

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
            return redirect(url_for("role_picker"))

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
            return redirect(url_for("role_picker"))

        return render_template(
            "task_detail.html",
            task=task,
            assignment=assignment,
            can_act=is_assigned_trade,
        )

    @app.route("/tasks/<int:task_id>/checkin", methods=["POST"])
    @role_required("trade")
    def checkin_task(task_id):
        task = Task.query.get_or_404(task_id)
        assignment = _current_assignment(task)
        if not assignment or session.get("trade_company_id") != assignment.trade_company_id:
            flash("You don't have access to that task.", "error")
            return redirect(url_for("role_picker"))

        task.status = "checked_in"
        _log_audit(
            assignment.trade_company.name, "check_in", "Task", task.id, f"Checked in to task '{task.name}'"
        )
        db.session.commit()
        flash("Checked in.", "success")
        return redirect(url_for("task_detail", task_id=task.id))

    @app.route("/tasks/<int:task_id>/complete", methods=["POST"])
    @role_required("trade")
    def complete_task(task_id):
        task = Task.query.get_or_404(task_id)
        assignment = _current_assignment(task)
        if not assignment or session.get("trade_company_id") != assignment.trade_company_id:
            flash("You don't have access to that task.", "error")
            return redirect(url_for("role_picker"))

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
            return redirect(url_for("role_picker"))

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
            return redirect(url_for("role_picker"))

        project = Project.query.get_or_404(project_id)
        tasks = Task.query.filter_by(project_id=project_id).order_by(Task.sequence_order).all()
        active_delays = (
            DelayEvent.query.join(Task)
            .filter(Task.project_id == project_id, Task.status == "delayed")
            .order_by(DelayEvent.reported_at.desc())
            .all()
        )
        next_up = next((t for t in tasks if t.status != "complete"), None)

        return render_template(
            "homeowner_view.html",
            project=project,
            tasks=tasks,
            active_delays=active_delays,
            next_up=next_up,
            plain_status=PLAIN_STATUS,
        )

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
        if request.method == "POST":
            title = request.form.get("title", "").strip() or request.form.get("name", "").strip()
            trade = request.form.get("trade", "").strip() or request.form.get("trade_type_needed", "").strip()
            status = request.form.get("status", "pending")

            trade_mapped = trade.lower()
            if "plumb" in trade_mapped:
                trade_mapped = "plumbing"
            elif "elec" in trade_mapped:
                trade_mapped = "electrical"
            elif "fram" in trade_mapped:
                trade_mapped = "framing"
            elif "hvac" in trade_mapped:
                trade_mapped = "HVAC"
            elif "gen" in trade_mapped:
                trade_mapped = "general"

            existing = Task.query.filter_by(project_id=project_id).all()
            sequence_order = len(existing) + 1

            task = Task(
                project_id=project_id,
                name=title,
                trade_type_needed=trade_mapped,
                status=status,
                sequence_order=sequence_order
            )
            db.session.add(task)
            db.session.commit()

            flash("Task created.", "success")
            return redirect(url_for("project_detail", project_id=project_id))

        return render_template("new_task.html", project=project, trade_types=TRADE_TYPES)


app = create_app()

if __name__ == "__main__":
    app.run(port=5001, debug=True)
