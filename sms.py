"""Notification sending stub.

No real Twilio account is wired up. `_send_sms` is where a real Twilio client
call would go later (`client.messages.create(...)`) — the shape of the call
(to, body) is kept realistic so that swap is a one-line change.

This module carries forward the quiet-hours + opt-out-aware send logic from
the original single-business prototype's `send_reminder`, adapted to the
Project/ConsentRecord shape used by the Construction Trade Coordination
Platform (a homeowner's consent is now tied to their Project, not a
Customer/Appointment).
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from models import ConsentRecord, Notification, db

try:
    from twilio.rest import Client
    TWILIO_AVAILABLE = True
except ImportError:
    TWILIO_AVAILABLE = False

# ASSUMPTION (flagged in reports/flask-dev.md): the Phase 1 data model has no
# per-project timezone field, so quiet-hours checks use this fixed default for
# every homeowner rather than each project's actual local timezone.
DEFAULT_HOMEOWNER_TIMEZONE = "America/New_York"

QUIET_HOURS_START = 8  # 8 AM local
QUIET_HOURS_END = 21  # 9 PM local


def _send_sms(to, body):
    """Sends SMS using Twilio if configured, or falls back to print logging."""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_PHONE_NUMBER")

    if TWILIO_AVAILABLE and account_sid and auth_token and from_number:
        try:
            client = Client(account_sid, auth_token)
            client.messages.create(
                to=to,
                from_=from_number,
                body=body
            )
            print(f"[TWILIO SMS SENT] to={to} body={body!r}")
            return
        except Exception as e:
            print(f"[TWILIO SMS ERROR] to={to} error={e!r}")

    # Fallback log print
    print(f"[SMS PRINT FALLBACK] to={to} body={body!r}")


def _within_quiet_hours(timezone_name):
    try:
        now_local = datetime.now(ZoneInfo(timezone_name))
        return QUIET_HOURS_START <= now_local.hour < QUIET_HOURS_END
    except Exception:
        now_local = datetime.now()
        return QUIET_HOURS_START <= now_local.hour < QUIET_HOURS_END


def notify_homeowner(project, event_type, message):
    """Send a homeowner-facing SMS update, honoring opt-out status and quiet
    hours — the same skip conditions as the original prototype's
    send_reminder(), now keyed off the project's ConsentRecord instead of a
    Customer's.

    Matches the original behavior: skipped sends are printed (for visibility
    during development) but do not create a Notification row, since nothing
    was actually sent to the homeowner. Call sites still need to
    db.session.commit() afterward.
    """
    consent = (
        ConsentRecord.query.filter_by(project_id=project.id)
        .order_by(ConsentRecord.consented_at.desc())
        .first()
    )
    if consent is None or consent.opted_out:
        print(f"[SMS SKIPPED] project={project.id} reason=opted_out_or_no_consent")
        return

    if not _within_quiet_hours(DEFAULT_HOMEOWNER_TIMEZONE):
        print(f"[SMS SKIPPED] project={project.id} reason=quiet_hours")
        return

    _send_sms(project.client_phone, message)
    db.session.add(
        Notification(
            recipient_kind="homeowner",
            recipient_ref=project.id,
            event_type=event_type,
            channel="sms",
            message=message,
        )
    )


def notify_trade(trade_company, event_type, message):
    """Log a trade-facing notification. Trades don't carry a TCPA ConsentRecord
    in this MVP (only homeowners do) — always send/log."""
    _send_sms(trade_company.phone, message)
    db.session.add(
        Notification(
            recipient_kind="trade",
            recipient_ref=trade_company.id,
            event_type=event_type,
            channel="sms",
            message=message,
        )
    )


def notify_gc(event_type, message):
    """Log a GC-facing notification. No phone/contact is modeled for the GC
    org in this single-org MVP — this just writes to the Notification log,
    visible on the GC dashboard/audit."""
    db.session.add(
        Notification(
            recipient_kind="gc",
            recipient_ref=None,
            event_type=event_type,
            channel="app",
            message=message,
        )
    )
