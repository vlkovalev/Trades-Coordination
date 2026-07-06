from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

TRADE_TYPES = ["plumbing", "electrical", "framing", "HVAC", "general"]
TASK_STATUSES = ["pending", "invited", "confirmed", "checked_in", "complete", "delayed"]
ASSIGNMENT_STATUSES = ["invited", "accepted", "declined"]


class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    address = db.Column(db.String(220), nullable=False)
    client_name = db.Column(db.String(120), nullable=False)
    client_phone = db.Column(db.String(20), nullable=True)
    client_email = db.Column(db.String(120), nullable=True)
    budget = db.Column(db.Float, nullable=True, default=0.0)
    status = db.Column(db.String(20), nullable=False, default="active")  # active/complete
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    tasks = db.relationship(
        "Task", backref="project", cascade="all, delete-orphan", order_by="Task.sequence_order"
    )


class TradeCompany(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    trade_type = db.Column(db.String(40), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    service_area = db.Column(db.String(160), nullable=True)

    @property
    def average_rating(self):
        if not self.reviews:
            return None
        return sum(r.rating for r in self.reviews) / len(self.reviews)

    @property
    def rating_display(self):
        avg = self.average_rating
        if avg is None:
            return "No reviews yet"
        return f"{avg:.1f}★ ({len(self.reviews)} review{'s' if len(self.reviews) != 1 else ''})"


class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    trade_type_needed = db.Column(db.String(40), nullable=False)
    name = db.Column(db.String(160), nullable=False)
    sequence_order = db.Column(db.Integer, nullable=False)
    depends_on_task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending")
    scheduled_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    depends_on = db.relationship("Task", remote_side=[id])
    assignments = db.relationship("Assignment", backref="task", cascade="all, delete-orphan")
    photos = db.relationship("CompletionPhoto", backref="task", cascade="all, delete-orphan")
    delay_events = db.relationship(
        "DelayEvent", backref="task", cascade="all, delete-orphan", order_by="DelayEvent.reported_at.desc()"
    )

    @property
    def title(self):
        return self.name

    @title.setter
    def title(self, value):
        self.name = value

    @property
    def trade(self):
        return self.trade_type_needed

    @trade.setter
    def trade(self, value):
        self.trade_type_needed = value



class Assignment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=False)
    trade_company_id = db.Column(db.Integer, db.ForeignKey("trade_company.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="invited")
    invited_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    responded_at = db.Column(db.DateTime, nullable=True)

    trade_company = db.relationship("TradeCompany")


class CompletionPhoto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    caption = db.Column(db.String(255), nullable=True)


class DelayEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    reported_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class ConsentRecord(db.Model):
    """TCPA consent capture — same shape as the prior single-business build,
    linked to a Project (via client_phone) instead of an Appointment/Customer."""

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    consent_text = db.Column(db.Text, nullable=False)
    consented_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    ip_address = db.Column(db.String(45), nullable=True)
    opted_out = db.Column(db.Boolean, nullable=False, default=False)
    opted_out_at = db.Column(db.DateTime, nullable=True)

    project = db.relationship("Project", backref="consent_records")


class Notification(db.Model):
    """Log of notifications sent (or that sms.py attempted to send). Makes the
    prototype's notifications visible in the UI instead of just printing."""

    id = db.Column(db.Integer, primary_key=True)
    recipient_kind = db.Column(db.String(20), nullable=False)  # trade / homeowner / gc
    recipient_ref = db.Column(db.Integer, nullable=True)  # trade_company.id, project.id, or None for gc
    event_type = db.Column(db.String(60), nullable=False)
    channel = db.Column(db.String(20), nullable=False)  # sms / app
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor = db.Column(db.String(160), nullable=False)  # free text: role + name/company
    action = db.Column(db.String(80), nullable=False)
    entity_type = db.Column(db.String(40), nullable=False)
    entity_id = db.Column(db.Integer, nullable=False)
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    detail = db.Column(db.Text, nullable=True)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # gc, trade, homeowner
    trade_company_id = db.Column(db.Integer, db.ForeignKey("trade_company.id"), nullable=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)

    trade_company = db.relationship("TradeCompany")
    project = db.relationship("Project")


class Review(db.Model):
    """A homeowner's review of a trade company that worked on their project.
    One review per (project, trade_company) pair — a homeowner updates their
    existing review rather than piling up duplicates if a trade does more
    than one task on the same project."""

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    trade_company_id = db.Column(db.Integer, db.ForeignKey("trade_company.id"), nullable=False)
    rating = db.Column(db.Integer, nullable=False)  # 1-5
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    project = db.relationship("Project", backref="reviews")
    trade_company = db.relationship("TradeCompany", backref="reviews")

    __table_args__ = (db.UniqueConstraint("project_id", "trade_company_id", name="uq_review_project_trade"),)

