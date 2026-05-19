import os
import resend

resend.api_key = os.environ.get("RESEND_API_KEY", "")

FROM_ADDRESS = os.environ.get("EMAIL_FROM", "MinuteAI <onboarding@resend.dev>")
APP_URL   = os.environ.get("APP_URL", "http://localhost:5173")
ADMIN_URL = os.environ.get("ADMIN_URL", "http://localhost:5174")


def send_minutes_ready(to_email: str, job_id: str, meeting_title: str):
    if not resend.api_key:
        return
    results_url = f"{APP_URL}/results/{job_id}"
    resend.Emails.send({
        "from": FROM_ADDRESS,
        "to": [to_email],
        "subject": f"Your minutes are ready — {meeting_title}",
        "html": f"""
        <div style="font-family:system-ui,sans-serif;max-width:520px;margin:40px auto;color:#374151">
          <div style="background:linear-gradient(135deg,#7c3aed,#4f46e5);padding:32px;border-radius:16px 16px 0 0;text-align:center">
            <h1 style="color:#fff;margin:0;font-size:24px;font-weight:700">MinuteAI</h1>
            <p style="color:#ddd6fe;margin:8px 0 0">Your meeting minutes are ready</p>
          </div>
          <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 16px 16px;padding:32px">
            <p style="font-size:15px;line-height:1.6;margin:0 0 16px">
              Your minutes for <strong>{meeting_title}</strong> have been formatted and are ready to download.
            </p>
            <a href="{results_url}"
               style="display:inline-block;background:#7c3aed;color:#fff;text-decoration:none;
                      padding:12px 28px;border-radius:10px;font-weight:600;font-size:15px;margin-bottom:24px">
              View &amp; Download Minutes &rarr;
            </a>
            <p style="font-size:12px;color:#9ca3af;margin:0">
              This link is valid for 24 hours. After that, re-upload your recording to generate new minutes.
            </p>
          </div>
        </div>
        """,
    })


def send_attendee_recap(
    to_email: str,
    job_id: str,
    meeting_title: str,
    recorded_by_name: str | None,
    recorded_by_email: str | None,
):
    if not resend.api_key:
        return
    results_url = f"{APP_URL}/results/{job_id}"
    recorder    = recorded_by_name or recorded_by_email or "Someone"
    resend.Emails.send({
        "from": FROM_ADDRESS,
        "to": [to_email],
        "subject": f"Meeting recap: {meeting_title}",
        "html": f"""
        <div style="font-family:system-ui,sans-serif;max-width:520px;margin:40px auto;color:#374151">
          <div style="background:linear-gradient(135deg,#7c3aed,#4f46e5);padding:32px;border-radius:16px 16px 0 0;text-align:center">
            <h1 style="color:#fff;margin:0;font-size:24px;font-weight:700">MinuteAI</h1>
            <p style="color:#ddd6fe;margin:8px 0 0">Your meeting recap is ready</p>
          </div>
          <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 16px 16px;padding:32px">
            <p style="font-size:15px;line-height:1.6;margin:0 0 6px">
              <strong>{recorder}</strong> used MinuteAI to record your meeting.
            </p>
            <p style="font-size:15px;line-height:1.6;margin:0 0 24px;color:#6b7280">
              The recap for <strong>{meeting_title}</strong> is now available.
            </p>
            <a href="{results_url}"
               style="display:inline-block;background:#7c3aed;color:#fff;text-decoration:none;
                      padding:12px 28px;border-radius:10px;font-weight:600;font-size:15px;margin-bottom:28px">
              View meeting recap &rarr;
            </a>
            <hr style="border:none;border-top:1px solid #f1f5f9;margin:0 0 16px">
            <p style="font-size:12px;color:#9ca3af;margin:0">
              Meeting notes recorded on behalf of {recorded_by_email or recorder}.
            </p>
          </div>
        </div>
        """,
    })


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
              Go to Admin → Bot Config and click <strong>Sign In Bot Account</strong> to restore the session.
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
