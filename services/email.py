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
