"""
COP Engine — Email Notification Service
========================================
Skickar e-postnotifieringar vid schemaändringar och frånvaro.
Använder aiosmtplib för async SMTP, med fallback till loggning.
"""

import os
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("cop.email")

email_router = APIRouter(prefix="/notifications", tags=["Notifications"])

# === SMTP Configuration ===
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "cop@ortoportal.cloud")
SMTP_TLS = os.getenv("SMTP_TLS", "true").lower() in ("true", "1", "yes")

# In-memory notification log
_notification_log: list[dict] = []


# === Models ===

class NotificationRequest(BaseModel):
    to_emails: list[str] = Field(description="Mottagare")
    subject: str = Field(description="Ämne")
    body: str = Field(description="Meddelande")
    notification_type: str = Field(default="info", description="Typ: schedule_change, absence, info")


class ScheduleChangeNotification(BaseModel):
    schedule_id: str
    clinic_id: str
    change_type: str = Field(description="Typ: generated, adjusted, reoptimized")
    affected_doctors: list[str] = Field(default_factory=list)
    summary: str = Field(default="")


class AbsenceNotification(BaseModel):
    doctor_id: str
    doctor_name: str
    absence_type: str
    start_date: str
    end_date: str
    replacement_chain: Optional[dict] = None


# === Email sending ===

async def _send_email(to: str, subject: str, html_body: str) -> bool:
    """Skicka e-post via SMTP. Returnerar True om lyckat."""
    if not SMTP_HOST:
        logger.info(f"[DRY-RUN] Email to={to}, subject={subject}")
        _notification_log.append({
            "to": to,
            "subject": subject,
            "status": "dry_run",
            "timestamp": datetime.utcnow().isoformat(),
        })
        return True

    try:
        import aiosmtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        msg = MIMEMultipart("alternative")
        msg["From"] = SMTP_FROM
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        await aiosmtplib.send(
            msg,
            hostname=SMTP_HOST,
            port=SMTP_PORT,
            username=SMTP_USER,
            password=SMTP_PASS,
            use_tls=SMTP_TLS,
        )

        _notification_log.append({
            "to": to,
            "subject": subject,
            "status": "sent",
            "timestamp": datetime.utcnow().isoformat(),
        })
        logger.info(f"Email sent to {to}: {subject}")
        return True

    except Exception as e:
        logger.error(f"Failed to send email to {to}: {e}")
        _notification_log.append({
            "to": to,
            "subject": subject,
            "status": "failed",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat(),
        })
        return False


def _schedule_change_html(data: ScheduleChangeNotification) -> str:
    """Generera HTML för schemaändringsnotifiering."""
    change_labels = {
        "generated": "Nytt schema genererat",
        "adjusted": "Schema justerat",
        "reoptimized": "Schema omoptimerat",
    }
    title = change_labels.get(data.change_type, "Schemaändring")
    doctors_html = ""
    if data.affected_doctors:
        doctors_html = "<ul>" + "".join(f"<li>{d}</li>" for d in data.affected_doctors) + "</ul>"

    return f"""
    <div style="font-family: system-ui, -apple-system, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background: linear-gradient(135deg, #1e293b, #0f172a); padding: 20px 24px; border-radius: 12px 12px 0 0;">
            <h1 style="color: white; font-size: 18px; margin: 0;">COP — {title}</h1>
        </div>
        <div style="background: #f8fafc; padding: 24px; border: 1px solid #e2e8f0; border-top: none; border-radius: 0 0 12px 12px;">
            <p style="color: #334155; font-size: 14px; margin: 0 0 12px 0;">
                <strong>Klinik:</strong> {data.clinic_id}<br>
                <strong>Schema-ID:</strong> {data.schedule_id[:12]}
            </p>
            {f'<p style="color: #334155; font-size: 14px;">{data.summary}</p>' if data.summary else ''}
            {f'<p style="color: #64748b; font-size: 13px;"><strong>Berörda läkare:</strong></p>{doctors_html}' if doctors_html else ''}
            <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 16px 0;">
            <p style="color: #94a3b8; font-size: 11px; margin: 0;">
                COP Engine — Clinical Operations Protocol<br>
                {datetime.now().strftime('%Y-%m-%d %H:%M')}
            </p>
        </div>
    </div>
    """


def _absence_html(data: AbsenceNotification) -> str:
    """Generera HTML för frånvaronotifiering."""
    absence_labels = {
        "sjuk": "Sjukdom", "semester": "Semester", "vab": "VAB",
        "utbildning": "Utbildning", "konferens": "Konferens",
    }
    chain_html = ""
    if data.replacement_chain and data.replacement_chain.get("replacements"):
        chain_html = "<h3 style='color: #334155; font-size: 14px;'>Ersättningskedja:</h3><ul>"
        for r in data.replacement_chain["replacements"]:
            chain_html += f"<li>{r.get('original_function', '?')}: {r.get('replacement_name', r.get('replacement_id', '?'))}</li>"
        chain_html += "</ul>"

    return f"""
    <div style="font-family: system-ui, -apple-system, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background: linear-gradient(135deg, #7c3aed, #5b21b6); padding: 20px 24px; border-radius: 12px 12px 0 0;">
            <h1 style="color: white; font-size: 18px; margin: 0;">COP — Frånvaro registrerad</h1>
        </div>
        <div style="background: #f8fafc; padding: 24px; border: 1px solid #e2e8f0; border-top: none; border-radius: 0 0 12px 12px;">
            <p style="color: #334155; font-size: 14px; margin: 0 0 12px 0;">
                <strong>{data.doctor_name}</strong> är frånvarande<br>
                <strong>Typ:</strong> {absence_labels.get(data.absence_type, data.absence_type)}<br>
                <strong>Period:</strong> {data.start_date} — {data.end_date}
            </p>
            {chain_html}
            <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 16px 0;">
            <p style="color: #94a3b8; font-size: 11px; margin: 0;">
                COP Engine — Clinical Operations Protocol<br>
                {datetime.now().strftime('%Y-%m-%d %H:%M')}
            </p>
        </div>
    </div>
    """


# === API Routes ===

@email_router.post("/send")
async def send_notification(req: NotificationRequest):
    """Skicka en notifiering till en eller flera mottagare."""
    results = []
    for email in req.to_emails:
        ok = await _send_email(email, req.subject, f"<p>{req.body}</p>")
        results.append({"email": email, "sent": ok})
    return {"results": results}


@email_router.post("/schedule-change")
async def notify_schedule_change(data: ScheduleChangeNotification):
    """Skicka notifiering om schemaändring till berörda."""
    html = _schedule_change_html(data)
    subject = f"COP: Schema {data.change_type} — {data.clinic_id}"

    # In production, fetch affected doctors' emails from user DB
    db = get_db()
    sent_to = []
    for doctor_id in data.affected_doctors:
        user = await db.get_user(doctor_id)
        if user and user.get("email"):
            ok = await _send_email(user["email"], subject, html)
            sent_to.append({"doctor_id": doctor_id, "email": user["email"], "sent": ok})
        else:
            logger.info(f"No email for doctor {doctor_id}, skipping notification")

    return {"notifications_sent": len(sent_to), "details": sent_to}


@email_router.post("/absence")
async def notify_absence(data: AbsenceNotification):
    """Skicka notifiering om frånvaro."""
    html = _absence_html(data)
    subject = f"COP: Frånvaro — {data.doctor_name} ({data.absence_type})"

    # Send to all admin/scheduler users
    db = get_db()
    users = await db.list_users()
    sent_to = []
    for user in users:
        if user.get("role") in ("admin", "scheduler") and user.get("email"):
            ok = await _send_email(user["email"], subject, html)
            sent_to.append({"user_id": user["user_id"], "email": user["email"], "sent": ok})

    return {"notifications_sent": len(sent_to), "details": sent_to}


@email_router.get("/log")
async def get_notification_log(limit: int = 50):
    """Hämta senaste notifieringsloggen."""
    return {"notifications": _notification_log[-limit:], "total": len(_notification_log)}


@email_router.get("/status")
async def email_status():
    """Kontrollera e-postkonfiguration."""
    return {
        "smtp_configured": bool(SMTP_HOST),
        "smtp_host": SMTP_HOST or "(not configured — dry-run mode)",
        "smtp_from": SMTP_FROM,
        "total_sent": sum(1 for n in _notification_log if n.get("status") == "sent"),
        "total_dry_run": sum(1 for n in _notification_log if n.get("status") == "dry_run"),
        "total_failed": sum(1 for n in _notification_log if n.get("status") == "failed"),
    }
