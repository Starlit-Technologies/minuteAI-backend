import os
import resend

resend.api_key = os.environ.get("RESEND_API_KEY", "")

FROM_ADDRESS = os.environ.get("EMAIL_FROM", "MinuteAI <onboarding@resend.dev>")
APP_URL   = os.environ.get("APP_URL", "http://localhost:5173")
ADMIN_URL = os.environ.get("ADMIN_URL", "http://localhost:5174")


def _stat_card(count: int, label: str, bg: str, color: str) -> str:
    return f"""
    <td style="width:33%;padding:0 6px;text-align:center;vertical-align:top">
      <div style="background:{bg};border-radius:10px;padding:14px 8px">
        <div style="font-size:20px;font-weight:700;color:{color};line-height:1">{count}+</div>
        <div style="font-size:11px;color:{color};margin-top:4px;line-height:1.3">{label}</div>
      </div>
    </td>"""


def _topic_chip(topic: str) -> str:
    return (
        f'<span style="display:inline-block;background:#f3f4f6;border:1px solid #e5e7eb;'
        f'border-radius:20px;padding:4px 12px;font-size:12px;color:#374151;margin:3px 2px">'
        f'{topic}</span>'
    )


def _bullet(text: str) -> str:
    return (
        f'<li style="margin-bottom:10px;font-size:14px;line-height:1.6;color:#374151">'
        f'{text}</li>'
    )


def send_minutes_ready(
    to_email: str,
    job_id: str,
    meeting_title: str,
    minutes: dict | None = None,
    user_name: str | None = None,
    user_email: str | None = None,
):
    if not resend.api_key:
        return

    results_url = f"{APP_URL}/results/{job_id}"
    share_url   = results_url
    title       = meeting_title or "Meeting"
    mins        = minutes or {}
    meta        = mins.get("metadata") or {}

    # ── stats ──────────────────────────────────────────────────────────────
    action_count   = len(mins.get("actionItems") or [])
    question_count = len(mins.get("questionsRaised") or [])
    decision_count = len(mins.get("keyDecisions") or [])
    date_str       = meta.get("date") or ""
    duration_str   = meta.get("duration") or ""

    stats_row = ""
    if question_count or action_count or decision_count:
        stats_row = f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="margin:20px 0">
          <tr>
            {_stat_card(question_count, "Questions were asked", "#fffbeb", "#92400e") if question_count else ""}
            {_stat_card(action_count,   "Action items detected", "#ede9fe", "#5b21b6") if action_count else ""}
            {_stat_card(decision_count, "Key decisions made",    "#fce7f3", "#9d174d") if decision_count else ""}
          </tr>
        </table>"""

    # ── topics ─────────────────────────────────────────────────────────────
    sections     = mins.get("sections") or []
    topic_names  = [s.get("title") for s in sections if s.get("title")][:6]
    topics_html  = ""
    if topic_names:
        chips       = "".join(_topic_chip(t) for t in topic_names)
        extra_count = max(0, len(sections) - 6)
        extra_chip  = _topic_chip(f"+{extra_count} more") if extra_count else ""
        topics_html = f"""
        <div style="margin:20px 0">
          <p style="font-size:11px;font-weight:600;color:#9ca3af;letter-spacing:.06em;
                    text-transform:uppercase;margin:0 0 10px">Discussed in this meeting</p>
          <div>{chips}{extra_chip}</div>
        </div>"""

    # ── overview bullets ────────────────────────────────────────────────────
    overview     = mins.get("overview") or ""
    bullets_html = ""
    if overview:
        sentences = [s.strip() for s in overview.replace(". ", ".|").split("|") if s.strip()][:6]
        items     = "".join(_bullet(s) for s in sentences)
        bullets_html = f"""
        <div style="margin:24px 0">
          <h3 style="font-size:16px;font-weight:700;color:#111827;margin:0 0 12px">Meeting Overview</h3>
          <ul style="margin:0;padding-left:20px">{items}</ul>
        </div>"""

    # ── date / time pill ───────────────────────────────────────────────────
    meta_parts = " &middot; ".join(p for p in [date_str, duration_str] if p)
    meta_line  = (
        f'<p style="font-size:12px;color:#6b7280;margin:4px 0 0">{meta_parts}</p>'
        if meta_parts else ""
    )

    # ── recorder pill ──────────────────────────────────────────────────────
    recorder_name  = user_name or user_email or "Your team"
    recorder_email = user_email or to_email

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;padding:32px 16px">
  <tr><td align="center">
    <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px">

      <!-- logo row -->
      <tr><td style="padding-bottom:20px;text-align:center">
        <div style="display:inline-flex;align-items:center;gap:8px">
          <div style="width:32px;height:32px;background:linear-gradient(135deg,#7c3aed,#4f46e5);
                      border-radius:8px;display:inline-block"></div>
          <span style="font-size:18px;font-weight:700;color:#111827">MinuteAI</span>
        </div>
      </td></tr>

      <!-- main card -->
      <tr><td style="background:#ffffff;border-radius:16px;overflow:hidden;
                     box-shadow:0 1px 3px rgba(0,0,0,.08)">

        <!-- header band -->
        <div style="padding:32px 32px 24px;text-align:center;border-bottom:1px solid #f3f4f6">
          <div style="width:64px;height:64px;background:linear-gradient(135deg,#ede9fe,#ddd6fe);
                      border-radius:16px;margin:0 auto 16px;display:flex;align-items:center;justify-content:center">
            <div style="width:64px;height:64px;line-height:64px;text-align:center;font-size:28px">🎙️</div>
          </div>
          <p style="font-size:14px;color:#6b7280;margin:0 0 4px">
            <strong style="color:#111827">{recorder_name}</strong> recorded a meeting
          </p>
          <p style="font-size:13px;color:#9ca3af;margin:0">To record and take notes</p>
        </div>

        <!-- meeting card -->
        <div style="padding:20px 32px">
          <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:12px;
                      padding:16px 20px;display:flex;align-items:flex-start;gap:14px;margin-bottom:20px">
            <div style="width:36px;height:36px;background:#ede9fe;border-radius:8px;
                        display:inline-block;text-align:center;line-height:36px;font-size:16px;shrink:0">📅</div>
            <div>
              <p style="font-size:15px;font-weight:600;color:#111827;margin:0">{title}</p>
              {meta_line}
            </div>
          </div>

          <!-- CTA buttons -->
          <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px">
            <tr>
              <td style="padding-right:6px">
                <a href="{results_url}"
                   style="display:block;background:linear-gradient(135deg,#7c3aed,#4f46e5);color:#fff;
                          text-decoration:none;padding:12px 16px;border-radius:10px;font-weight:600;
                          font-size:14px;text-align:center">
                  View meeting recap
                </a>
              </td>
              <td style="padding-left:6px">
                <a href="{share_url}"
                   style="display:block;background:#f3f4f6;color:#7c3aed;text-decoration:none;
                          padding:12px 16px;border-radius:10px;font-weight:600;font-size:14px;
                          text-align:center;border:1px solid #e5e7eb">
                  Share this recap
                </a>
              </td>
            </tr>
          </table>
          <!-- Download links -->
          <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px">
            <tr>
              <td style="padding-right:6px">
                <a href="{APP_URL}/download/{job_id}/pdf"
                   style="display:block;background:#f9fafb;color:#374151;text-decoration:none;
                          padding:10px 16px;border-radius:10px;font-weight:500;font-size:13px;
                          text-align:center;border:1px solid #e5e7eb">
                  ⬇ Download PDF
                </a>
              </td>
              <td style="padding-left:6px">
                <a href="{APP_URL}/download/{job_id}/docx"
                   style="display:block;background:#f9fafb;color:#374151;text-decoration:none;
                          padding:10px 16px;border-radius:10px;font-weight:500;font-size:13px;
                          text-align:center;border:1px solid #e5e7eb">
                  ⬇ Download DOCX
                </a>
              </td>
            </tr>
          </table>
        </div>

        <!-- divider -->
        <div style="height:1px;background:#f3f4f6;margin:0 32px"></div>

        <!-- stats + topics + overview -->
        <div style="padding:20px 32px 28px">
          {"" if not stats_row else f'<p style="font-size:12px;font-weight:600;color:#9ca3af;letter-spacing:.06em;text-transform:uppercase;margin:0 0 12px">In this meeting</p>'}
          {stats_row}
          {topics_html}
          {bullets_html}

          <!-- view notes CTA -->
          <div style="text-align:center;margin-top:24px">
            <a href="{results_url}"
               style="display:inline-block;background:#f5f3ff;color:#7c3aed;text-decoration:none;
                      padding:12px 32px;border-radius:10px;font-weight:600;font-size:14px;
                      border:1px solid #ddd6fe">
              View complete meeting notes
            </a>
          </div>
        </div>

      </td></tr>

      <!-- footer -->
      <tr><td style="padding-top:24px;text-align:center">
        <p style="font-size:12px;color:#9ca3af;margin:0 0 6px">
          Meeting notes taken on behalf of {recorder_email}
        </p>
        <p style="font-size:11px;color:#d1d5db;margin:0">
          MinuteAI &middot; <a href="{APP_URL}" style="color:#d1d5db">minuteai.app</a>
        </p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""

    resend.Emails.send({
        "from": FROM_ADDRESS,
        "to": [to_email],
        "subject": f"Your meeting recap — {title}",
        "html": html,
    })


def send_attendee_recap(
    to_email: str,
    job_id: str,
    meeting_title: str,
    recorded_by_name: str | None,
    recorded_by_email: str | None,
    minutes: dict | None = None,
):
    if not resend.api_key:
        return
    send_minutes_ready(
        to_email=to_email,
        job_id=job_id,
        meeting_title=meeting_title,
        minutes=minutes,
        user_name=recorded_by_name,
        user_email=recorded_by_email,
    )


def send_bot_auth_alert(to_emails: list[str], bot_email: str | None, error: str):
    if not resend.api_key:
        return
    admin_url = f"{ADMIN_URL.rstrip('/')}/bot-config"
    bot_label = bot_email or "the bot account"
    resend.Emails.send({
        "from": FROM_ADDRESS,
        "to": to_emails,
        "subject": "Action required: MinuteAI bot needs re-authentication",
        "html": f"""
        <div style="font-family:system-ui,sans-serif;max-width:520px;margin:40px auto;color:#374151">
          <div style="background:linear-gradient(135deg,#dc2626,#b91c1c);padding:32px;border-radius:16px 16px 0 0;text-align:center">
            <h1 style="color:#fff;margin:0;font-size:24px;font-weight:700">MinuteAI</h1>
            <p style="color:#fecaca;margin:8px 0 0">Bot authentication has expired</p>
          </div>
          <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 16px 16px;padding:32px">
            <p style="font-size:15px;line-height:1.6;margin:0 0 12px">
              The Google session for <strong>{bot_label}</strong> is no longer valid.
              The bot will join future meetings as a guest and may be blocked from restricted calls.
            </p>
            <p style="font-size:13px;color:#6b7280;background:#fef2f2;border:1px solid #fecaca;
                      border-radius:8px;padding:12px;margin:0 0 20px">
              {error}
            </p>
            <a href="{admin_url}"
               style="display:inline-block;background:#7c3aed;color:#fff;text-decoration:none;
                      padding:12px 28px;border-radius:10px;font-weight:600;font-size:15px;margin-bottom:24px">
              Re-authenticate now &rarr;
            </a>
            <p style="font-size:12px;color:#9ca3af;margin:0">
              Go to Admin &rarr; Bot Config and click <strong>Sign In Bot Account</strong> to restore the session.
            </p>
          </div>
        </div>
        """,
    })


def send_error_email(to_email: str, job_id: str, error_message: str):
    if not resend.api_key:
        return
    processing_url = f"{APP_URL}/processing/{job_id}"
    resend.Emails.send({
        "from": FROM_ADDRESS,
        "to": [to_email],
        "subject": "Processing failed — retry your meeting minutes",
        "html": f"""
        <div style="font-family:system-ui,sans-serif;max-width:520px;margin:40px auto;color:#374151">
          <div style="background:linear-gradient(135deg,#dc2626,#b91c1c);padding:32px;border-radius:16px 16px 0 0;text-align:center">
            <h1 style="color:#fff;margin:0;font-size:24px;font-weight:700">MinuteAI</h1>
            <p style="color:#fecaca;margin:8px 0 0">Processing encountered an error</p>
          </div>
          <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 16px 16px;padding:32px">
            <p style="font-size:15px;line-height:1.6;margin:0 0 12px">
              Something went wrong while processing your meeting recording.
            </p>
            <p style="font-size:13px;color:#6b7280;background:#f9fafb;border:1px solid #e5e7eb;
                      border-radius:8px;padding:12px;margin:0 0 20px;font-family:monospace">
              {error_message}
            </p>
            <a href="{processing_url}"
               style="display:inline-block;background:#7c3aed;color:#fff;text-decoration:none;
                      padding:12px 28px;border-radius:10px;font-weight:600;font-size:15px;margin-bottom:24px">
              Retry from last step &rarr;
            </a>
            <p style="font-size:12px;color:#9ca3af;margin:0">
              MinuteAI will resume from the last successful step — no need to re-upload.
            </p>
          </div>
        </div>
        """,
    })


def _task_row(title: str, status: str, deadline: str | None, meeting: str | None) -> str:
    status_color = {"todo": "#6b7280", "in_progress": "#7c3aed", "done": "#059669"}.get(status, "#6b7280")
    status_label = {"todo": "To Do", "in_progress": "In Progress", "done": "Done"}.get(status, status)
    meta_parts = []
    if deadline:
        meta_parts.append(f"Due {deadline}")
    if meeting:
        meta_parts.append(meeting)
    meta = " &middot; ".join(meta_parts)
    return f"""
    <tr>
      <td style="padding:10px 0;border-bottom:1px solid #f3f4f6;vertical-align:top">
        <div style="font-size:14px;color:#111827;font-weight:500;line-height:1.4">{title}</div>
        {f'<div style="font-size:12px;color:#9ca3af;margin-top:3px">{meta}</div>' if meta else ''}
      </td>
      <td style="padding:10px 0 10px 16px;border-bottom:1px solid #f3f4f6;vertical-align:top;text-align:right;white-space:nowrap">
        <span style="display:inline-block;background:{status_color}1a;color:{status_color};
                     font-size:11px;font-weight:600;border-radius:20px;padding:3px 10px">
          {status_label}
        </span>
      </td>
    </tr>"""


def send_pre_meeting_brief(
    to_email: str,
    user_name: str | None,
    session_title: str,
    scheduled_for,
    attendees: list[str],
    last_minutes: dict | None,
    open_tasks: list[dict],
):
    if not resend.api_key:
        return

    name  = user_name or to_email
    title = session_title or "Upcoming Meeting"

    # ── scheduled time ─────────────────────────────────────────────────────
    time_str = ""
    if scheduled_for:
        try:
            d = scheduled_for
            hour = d.hour % 12 or 12
            ampm = "AM" if d.hour < 12 else "PM"
            time_str = f"{d.strftime('%A, %B')} {d.day} at {hour}:{d.minute:02d} {ampm} UTC"
        except Exception:
            time_str = str(scheduled_for)[:16]

    # ── attendees chips ────────────────────────────────────────────────────
    attendees_html = ""
    if attendees:
        chips = "".join(
            f'<span style="display:inline-block;background:#f3f4f6;border:1px solid #e5e7eb;'
            f'border-radius:20px;padding:3px 10px;font-size:12px;color:#374151;margin:2px">'
            f'{a}</span>'
            for a in attendees[:8]
        )
        attendees_html = f"""
        <div style="margin-bottom:20px">
          <p style="font-size:11px;font-weight:600;color:#9ca3af;letter-spacing:.06em;
                    text-transform:uppercase;margin:0 0 8px">Attendees</p>
          <div>{chips}</div>
        </div>"""

    # ── thread continuity ──────────────────────────────────────────────────
    continuity_html = ""
    if last_minutes:
        sections = []
        decisions = last_minutes.get("keyDecisions", [])[:3]
        if decisions:
            items = "".join(
                f'<li style="margin-bottom:7px;font-size:13px;color:#374151;line-height:1.5">'
                f'{d.get("decision", d) if isinstance(d, dict) else d}</li>'
                for d in decisions
            )
            sections.append(
                f'<p style="font-size:12px;font-weight:600;color:#7c3aed;margin:0 0 6px">Key decisions</p>'
                f'<ul style="margin:0 0 14px;padding-left:18px">{items}</ul>'
            )
        next_steps = last_minutes.get("nextSteps", [])[:3]
        if next_steps:
            items = "".join(
                f'<li style="margin-bottom:7px;font-size:13px;color:#374151;line-height:1.5">'
                f'{s.get("step", s) if isinstance(s, dict) else s}</li>'
                for s in next_steps
            )
            sections.append(
                f'<p style="font-size:12px;font-weight:600;color:#7c3aed;margin:0 0 6px">Planned next steps</p>'
                f'<ul style="margin:0;padding-left:18px">{items}</ul>'
            )
        if sections:
            last_title = last_minutes.get("title", "Last meeting")
            continuity_html = f"""
            <div style="background:#f5f3ff;border:1px solid #ddd6fe;border-radius:12px;
                        padding:16px 20px;margin-bottom:20px">
              <p style="font-size:11px;font-weight:600;color:#9ca3af;letter-spacing:.06em;
                        text-transform:uppercase;margin:0 0 12px">
                Continuity from &ldquo;{last_title}&rdquo;
              </p>
              {"".join(sections)}
            </div>"""

    # ── open tasks ─────────────────────────────────────────────────────────
    tasks_html = ""
    if open_tasks:
        rows = "".join(
            _task_row(t.get("title", ""), t.get("status", "todo"), t.get("deadline"), None)
            for t in open_tasks
        )
        tasks_html = f"""
        <div style="margin-bottom:20px">
          <p style="font-size:11px;font-weight:600;color:#9ca3af;letter-spacing:.06em;
                    text-transform:uppercase;margin:0 0 10px">Your open action items</p>
          <table width="100%" cellpadding="0" cellspacing="0">{rows}</table>
          <div style="margin-top:10px;text-align:right">
            <a href="{APP_URL}/tasks" style="font-size:12px;color:#7c3aed;text-decoration:none">
              View all tasks &rarr;
            </a>
          </div>
        </div>"""

    empty_note = (
        '<p style="font-size:13px;color:#9ca3af;text-align:center;margin:0 0 20px">'
        'No open action items — you\'re all caught up!</p>'
        if not open_tasks and not continuity_html else ""
    )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;padding:32px 16px">
  <tr><td align="center">
    <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px">

      <tr><td style="padding-bottom:20px;text-align:center">
        <div style="display:inline-flex;align-items:center;gap:8px">
          <div style="width:32px;height:32px;background:linear-gradient(135deg,#7c3aed,#4f46e5);
                      border-radius:8px;display:inline-block"></div>
          <span style="font-size:18px;font-weight:700;color:#111827">MinuteAI</span>
        </div>
      </td></tr>

      <tr><td style="background:#ffffff;border-radius:16px;overflow:hidden;
                     box-shadow:0 1px 3px rgba(0,0,0,.08)">

        <div style="background:linear-gradient(135deg,#0ea5e9,#2563eb);padding:28px 32px">
          <p style="font-size:12px;font-weight:600;color:rgba(255,255,255,0.7);
                    letter-spacing:.08em;text-transform:uppercase;margin:0 0 6px">
            Pre-Meeting Brief
          </p>
          <h1 style="font-size:20px;font-weight:700;color:#fff;margin:0 0 4px">{title}</h1>
          <p style="font-size:13px;color:rgba(255,255,255,0.8);margin:0">
            {f'Starting {time_str}' if time_str else 'Starting soon'}
          </p>
        </div>

        <div style="padding:24px 32px">

          {attendees_html}
          {continuity_html}
          {tasks_html}
          {empty_note}

          <div style="text-align:center;margin-top:8px">
            <a href="{APP_URL}/meetings"
               style="display:inline-block;background:linear-gradient(135deg,#0ea5e9,#2563eb);
                      color:#fff;text-decoration:none;padding:12px 32px;border-radius:10px;
                      font-weight:600;font-size:14px">
              View upcoming meetings &rarr;
            </a>
          </div>

        </div>

      </td></tr>

      <tr><td style="padding-top:20px;text-align:center">
        <p style="font-size:11px;color:#d1d5db;margin:0">
          Hi {name} &middot; MinuteAI &middot;
          <a href="{APP_URL}/settings" style="color:#d1d5db">Manage notifications</a>
        </p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""

    resend.Emails.send({
        "from": FROM_ADDRESS,
        "to": [to_email],
        "subject": f"Pre-meeting brief: {title}",
        "html": html,
    })


def send_daily_briefing(
    to_email: str,
    user_name: str | None,
    briefing: dict,
):
    """Send AI Chief of Staff daily morning briefing."""
    if not resend.api_key:
        return

    name = user_name.split()[0] if user_name else (to_email.split("@")[0])
    date_str = briefing.get("date", "Today")
    insight = briefing.get("ai_insight", "")
    todays_meetings = briefing.get("todays_meetings", [])
    overdue_tasks = briefing.get("overdue_tasks", [])
    open_task_count = briefing.get("open_task_count", 0)
    overdue_count = briefing.get("overdue_count", 0)

    # ── stat pills ─────────────────────────────────────────────────────────
    def _pill(value, label, bg, color):
        return (
            f'<div style="background:{bg};border-radius:10px;padding:14px 8px;text-align:center">'
            f'<div style="font-size:22px;font-weight:700;color:{color};line-height:1">{value}</div>'
            f'<div style="font-size:11px;color:{color};margin-top:4px">{label}</div>'
            f'</div>'
        )

    stats_html = f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="margin:20px 0">
      <tr>
        <td style="width:33%;padding:0 5px">
          {_pill(len(todays_meetings), "meeting" + ("s" if len(todays_meetings) != 1 else "") + " today", "#ede9fe", "#7c3aed")}
        </td>
        <td style="width:33%;padding:0 5px">
          {_pill(open_task_count, "open tasks", "#f0fdf4", "#059669")}
        </td>
        <td style="width:33%;padding:0 5px">
          {_pill(overdue_count, "overdue", "#fef2f2" if overdue_count else "#f9fafb", "#dc2626" if overdue_count else "#9ca3af")}
        </td>
      </tr>
    </table>"""

    # ── today's meetings ───────────────────────────────────────────────────
    meetings_html = ""
    if todays_meetings:
        def _meeting_row(m):
            time_tag = (
                f'<span style="font-size:12px;color:#6b7280;margin-left:8px">'
                f'{m["scheduled_for"]}</span>'
            ) if m.get("scheduled_for") else ""
            return (
                f'<tr><td style="padding:8px 0;border-bottom:1px solid #f3f4f6">'
                f'<span style="font-size:14px;font-weight:600;color:#111827">{m["title"]}</span>'
                f'{time_tag}'
                f'</td></tr>'
            )
        rows = "".join(_meeting_row(m) for m in todays_meetings)
        meetings_html = f"""
        <div style="margin-bottom:20px">
          <p style="font-size:11px;font-weight:600;color:#9ca3af;letter-spacing:.06em;
                    text-transform:uppercase;margin:0 0 10px">Today's meetings</p>
          <table width="100%" cellpadding="0" cellspacing="0">{rows}</table>
        </div>"""

    # ── overdue tasks ──────────────────────────────────────────────────────
    overdue_html = ""
    if overdue_tasks:
        rows = "".join(
            f'<tr><td style="padding:8px 0;border-bottom:1px solid #fecaca;vertical-align:top">'
            f'<div style="font-size:13px;color:#111827;font-weight:500">{t["title"]}</div>'
            f'<div style="font-size:11px;color:#dc2626;margin-top:2px">'
            f'Due {t["deadline"]}{"  ·  " + str(t["days_overdue"]) + " days overdue" if t.get("days_overdue") else ""}'
            f'</div>'
            f'</td></tr>'
            for t in overdue_tasks
        )
        overdue_html = f"""
        <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:12px;
                    padding:16px 20px;margin-bottom:20px">
          <p style="font-size:11px;font-weight:600;color:#dc2626;letter-spacing:.06em;
                    text-transform:uppercase;margin:0 0 10px">⚠ Overdue tasks</p>
          <table width="100%" cellpadding="0" cellspacing="0">{rows}</table>
          <a href="{APP_URL}/tasks" style="font-size:12px;color:#dc2626;text-decoration:none;
                    display:inline-block;margin-top:8px">View all tasks →</a>
        </div>"""

    # ── AI insight card ────────────────────────────────────────────────────
    insight_html = ""
    if insight:
        insight_html = f"""
        <div style="background:#f5f3ff;border:1px solid #ddd6fe;border-radius:12px;
                    padding:16px 20px;margin-bottom:20px">
          <p style="font-size:11px;font-weight:600;color:#7c3aed;letter-spacing:.06em;
                    text-transform:uppercase;margin:0 0 8px">✦ Your morning insight</p>
          <p style="font-size:14px;color:#374151;line-height:1.65;margin:0">{insight}</p>
        </div>"""

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;padding:32px 16px">
  <tr><td align="center">
    <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px">

      <tr><td style="padding-bottom:20px;text-align:center">
        <div style="display:inline-flex;align-items:center;gap:8px">
          <div style="width:32px;height:32px;background:linear-gradient(135deg,#7c3aed,#4f46e5);
                      border-radius:8px;display:inline-block"></div>
          <span style="font-size:18px;font-weight:700;color:#111827">MinuteAI</span>
        </div>
      </td></tr>

      <tr><td style="background:#ffffff;border-radius:16px;overflow:hidden;
                     box-shadow:0 1px 3px rgba(0,0,0,.08)">

        <div style="background:linear-gradient(135deg,#4f46e5,#7c3aed);padding:28px 32px">
          <p style="font-size:12px;font-weight:600;color:rgba(255,255,255,0.7);
                    letter-spacing:.08em;text-transform:uppercase;margin:0 0 6px">
            Chief of Staff · Morning Briefing
          </p>
          <h1 style="font-size:22px;font-weight:700;color:#fff;margin:0 0 4px">
            Good morning, {name}
          </h1>
          <p style="font-size:13px;color:rgba(255,255,255,0.75);margin:0">{date_str}</p>
        </div>

        <div style="padding:24px 32px">
          {stats_html}
          {insight_html}
          {meetings_html}
          {overdue_html}

          <div style="text-align:center;margin-top:16px">
            <a href="{APP_URL}/dashboard"
               style="display:inline-block;background:linear-gradient(135deg,#4f46e5,#7c3aed);
                      color:#fff;text-decoration:none;padding:12px 32px;border-radius:10px;
                      font-weight:600;font-size:14px">
              Open dashboard →
            </a>
          </div>
        </div>

      </td></tr>

      <tr><td style="padding-top:20px;text-align:center">
        <p style="font-size:11px;color:#d1d5db;margin:0">
          MinuteAI Chief of Staff &middot;
          <a href="{APP_URL}/settings" style="color:#d1d5db">Manage notifications</a>
        </p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""

    resend.Emails.send({
        "from": FROM_ADDRESS,
        "to": [to_email],
        "subject": f"Your morning briefing — {date_str}",
        "html": html,
    })


def send_weekly_digest(
    to_email: str,
    user_name: str | None,
    week_label: str | None = None,
    overdue: list[dict] | None = None,
    due_this_week: list[dict] | None = None,
    other_open: list[dict] | None = None,
    completed_last_week: list[dict] | None = None,
):
    """Send a rich weekly action item digest with categorised tasks."""
    if not resend.api_key:
        return

    overdue           = overdue or []
    due_this_week     = due_this_week or []
    other_open        = other_open or []
    completed_last_week = completed_last_week or []
    total_open        = len(overdue) + len(due_this_week) + len(other_open)

    if total_open == 0 and not completed_last_week:
        return

    name = (user_name or to_email).split()[0]
    week = week_label or "this week"

    def _section(tasks: list[dict], header: str, header_color: str, bg: str, border: str) -> str:
        if not tasks:
            return ""
        rows = "".join(
            _task_row(t.get("title", ""), t.get("status", "todo"), t.get("deadline"), t.get("meeting_title"))
            for t in tasks
        )
        return f"""
        <div style="background:{bg};border:1px solid {border};border-radius:12px;
                    padding:16px 20px;margin-bottom:16px">
          <p style="font-size:11px;font-weight:700;color:{header_color};letter-spacing:.07em;
                    text-transform:uppercase;margin:0 0 10px">{header}</p>
          <table width="100%" cellpadding="0" cellspacing="0">{rows}</table>
        </div>"""

    def _done_row(t: dict) -> str:
        meeting_tag = (
            f'<span style="font-size:11px;color:#9ca3af;margin-left:8px">'
            f'from {t["meeting_title"]}</span>'
        ) if t.get("meeting_title") else ""
        return (
            f'<tr><td style="padding:7px 0;border-bottom:1px solid #f3f4f6">'
            f'<span style="font-size:13px;color:#6b7280;text-decoration:line-through">'
            f'{t.get("title","")}</span>'
            f'{meeting_tag}'
            f'</td></tr>'
        )

    overdue_section     = _section(overdue,       "⚠ Overdue",          "#dc2626", "#fef2f2", "#fecaca")
    due_section         = _section(due_this_week,  "Due this week",       "#b45309", "#fffbeb", "#fde68a")
    other_section       = _section(other_open,     "Other open tasks",    "#374151", "#f9fafb", "#e5e7eb")
    completed_rows      = "".join(_done_row(t) for t in completed_last_week)
    completed_section   = f"""
        <div style="margin-bottom:16px">
          <p style="font-size:11px;font-weight:700;color:#059669;letter-spacing:.07em;
                    text-transform:uppercase;margin:0 0 10px">✓ Completed last week ({len(completed_last_week)})</p>
          <table width="100%" cellpadding="0" cellspacing="0">{completed_rows}</table>
        </div>""" if completed_last_week else ""

    stats_html = f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 20px">
      <tr>
        <td style="width:33%;padding:0 5px">
          <div style="background:#fef2f2;border-radius:10px;padding:14px 8px;text-align:center">
            <div style="font-size:22px;font-weight:700;color:#dc2626;line-height:1">{len(overdue)}</div>
            <div style="font-size:11px;color:#dc2626;margin-top:4px">Overdue</div>
          </div>
        </td>
        <td style="width:33%;padding:0 5px">
          <div style="background:#fffbeb;border-radius:10px;padding:14px 8px;text-align:center">
            <div style="font-size:22px;font-weight:700;color:#b45309;line-height:1">{len(due_this_week)}</div>
            <div style="font-size:11px;color:#b45309;margin-top:4px">Due this week</div>
          </div>
        </td>
        <td style="width:33%;padding:0 5px">
          <div style="background:#f0fdf4;border-radius:10px;padding:14px 8px;text-align:center">
            <div style="font-size:22px;font-weight:700;color:#059669;line-height:1">{len(completed_last_week)}</div>
            <div style="font-size:11px;color:#059669;margin-top:4px">Completed</div>
          </div>
        </td>
      </tr>
    </table>"""

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;padding:32px 16px">
  <tr><td align="center">
    <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px">

      <tr><td style="padding-bottom:20px;text-align:center">
        <div style="display:inline-flex;align-items:center;gap:8px">
          <div style="width:32px;height:32px;background:linear-gradient(135deg,#7c3aed,#4f46e5);
                      border-radius:8px;display:inline-block"></div>
          <span style="font-size:18px;font-weight:700;color:#111827">MinuteAI</span>
        </div>
      </td></tr>

      <tr><td style="background:#ffffff;border-radius:16px;overflow:hidden;
                     box-shadow:0 1px 3px rgba(0,0,0,.08)">

        <div style="background:linear-gradient(135deg,#7c3aed,#4f46e5);padding:28px 32px">
          <p style="font-size:12px;font-weight:600;color:rgba(255,255,255,0.7);
                    letter-spacing:.08em;text-transform:uppercase;margin:0 0 6px">Weekly digest</p>
          <h1 style="font-size:22px;font-weight:700;color:#fff;margin:0 0 4px">
            Action items — {week}
          </h1>
          <p style="font-size:14px;color:rgba(255,255,255,0.75);margin:0">
            Hi {name}, you have {total_open} open task{'s' if total_open != 1 else ''}.
          </p>
        </div>

        <div style="padding:24px 32px">
          {stats_html}
          {overdue_section}
          {due_section}
          {other_section}
          {completed_section}
          <div style="text-align:center;margin-top:20px">
            <a href="{APP_URL}/tasks"
               style="display:inline-block;background:linear-gradient(135deg,#7c3aed,#4f46e5);
                      color:#fff;text-decoration:none;padding:12px 32px;border-radius:10px;
                      font-weight:600;font-size:14px">
              View all tasks &rarr;
            </a>
          </div>
        </div>

      </td></tr>

      <tr><td style="padding-top:20px;text-align:center">
        <p style="font-size:11px;color:#d1d5db;margin:0">
          Hi {name} &middot; MinuteAI &middot;
          <a href="{APP_URL}/settings" style="color:#d1d5db">Manage notifications</a>
        </p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""

    resend.Emails.send({
        "from": FROM_ADDRESS,
        "to": [to_email],
        "subject": f"Your weekly task digest — {week}",
        "html": html,
    })
