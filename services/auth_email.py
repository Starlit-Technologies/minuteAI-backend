import os
import logging
import resend

resend.api_key = os.environ.get("RESEND_API_KEY", "")

_FROM = os.environ.get("EMAIL_FROM", "MinuteAI <noreply@minuteai.app>")
_APP_URL = os.environ.get("APP_URL", "http://localhost:5173")

log = logging.getLogger(__name__)


def _base_template(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #f8f7ff; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}
    .wrapper {{ max-width: 580px; margin: 40px auto; background: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 4px 24px rgba(109,40,217,0.08); }}
    .header {{ background: linear-gradient(135deg, #7c3aed 0%, #4f46e5 100%); padding: 36px 40px 32px; text-align: center; }}
    .header-logo {{ display: flex; align-items: center; justify-content: center; gap: 10px; margin-bottom: 0; }}
    .header-logo-icon {{ width: 36px; height: 36px; background: rgba(255,255,255,0.2); border-radius: 10px; display: flex; align-items: center; justify-content: center; }}
    .header-logo-text {{ color: #ffffff; font-size: 20px; font-weight: 700; letter-spacing: -0.3px; }}
    .body {{ padding: 40px; }}
    .body h1 {{ font-size: 22px; font-weight: 700; color: #111827; margin-bottom: 12px; letter-spacing: -0.3px; }}
    .body p {{ font-size: 15px; color: #4b5563; line-height: 1.6; margin-bottom: 16px; }}
    .otp-block {{ background: linear-gradient(135deg, #f5f3ff, #eef2ff); border: 1px solid #ddd6fe; border-radius: 12px; padding: 24px; text-align: center; margin: 24px 0; }}
    .otp-label {{ font-size: 11px; font-weight: 600; letter-spacing: 1.5px; text-transform: uppercase; color: #7c3aed; margin-bottom: 10px; }}
    .otp-code {{ font-size: 42px; font-weight: 800; letter-spacing: 10px; color: #1e1b4b; font-variant-numeric: tabular-nums; }}
    .otp-expiry {{ font-size: 12px; color: #6b7280; margin-top: 10px; }}
    .btn {{ display: inline-block; background: linear-gradient(135deg, #7c3aed, #4f46e5); color: #ffffff; text-decoration: none; padding: 14px 32px; border-radius: 10px; font-weight: 600; font-size: 15px; margin: 8px 0; }}
    .divider {{ border: none; border-top: 1px solid #f3f4f6; margin: 28px 0; }}
    .note {{ background: #f9fafb; border-radius: 10px; padding: 16px 20px; font-size: 13px; color: #6b7280; line-height: 1.5; }}
    .footer {{ background: #f9fafb; padding: 24px 40px; text-align: center; border-top: 1px solid #f3f4f6; }}
    .footer p {{ font-size: 12px; color: #9ca3af; line-height: 1.6; }}
    .footer a {{ color: #7c3aed; text-decoration: none; }}
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="header">
      <div class="header-logo">
        <span class="header-logo-text">MinuteAI</span>
      </div>
    </div>
    <div class="body">
      {body}
    </div>
    <div class="footer">
      <p>You're receiving this email because an account was created at <a href="{_APP_URL}">MinuteAI</a>.<br />
      If you didn't request this, you can safely ignore this email.</p>
      <p style="margin-top:8px;">© 2025 MinuteAI. All rights reserved.</p>
    </div>
  </div>
</body>
</html>"""


def send_verification_otp(to_email: str, name: str, otp: str) -> None:
    first = name.split()[0] if name else "there"
    body = f"""
      <h1>Verify your email address</h1>
      <p>Hi {first}, welcome to MinuteAI! To complete your account setup, enter the verification code below.</p>
      <div class="otp-block">
        <div class="otp-label">Your verification code</div>
        <div class="otp-code">{otp}</div>
        <div class="otp-expiry">Expires in 15 minutes</div>
      </div>
      <p>Enter this code on the verification page to activate your account.</p>
      <hr class="divider" />
      <div class="note">
        <strong>Didn't create an account?</strong> Someone may have entered your email address by mistake. You can safely ignore this email — no account will be created without verification.
      </div>
    """
    resend.Emails.send({
        "from": _FROM,
        "to": [to_email],
        "subject": f"{otp} is your MinuteAI verification code",
        "html": _base_template("Verify your email — MinuteAI", body),
    })


def send_trial_ending_notice(to_email: str, name: str, trial_end_label: str, plan_name: str) -> None:
    first = name.split()[0] if name else "there"
    billing_url = f"{_APP_URL}/billing"
    body = f"""
      <h1>Your MinuteAI trial ends soon</h1>
      <p>Hi {first}, your free month of MinuteAI {plan_name} ends on <strong>{trial_end_label}</strong>.</p>
      <p>After the trial, your saved card will be charged automatically for your subscription unless you cancel before then.</p>
      <div style="text-align:center;margin:28px 0;">
        <a href="{billing_url}" class="btn">Manage billing</a>
      </div>
      <hr class="divider" />
      <div class="note">
        We are sending this notice at least three days before your trial ends so you have time to review or cancel.
      </div>
    """
    resend.Emails.send({
        "from": _FROM,
        "to": [to_email],
        "subject": "Your MinuteAI trial ends in 3 days",
        "html": _base_template("Your trial ends soon - MinuteAI", body),
    })


def send_login_otp(to_email: str, name: str, otp: str) -> None:
    first = name.split()[0] if name else "there"
    body = f"""
      <h1>Confirm your sign-in</h1>
      <p>Hi {first}, enter the code below to finish signing in to MinuteAI.</p>
      <div class="otp-block">
        <div class="otp-label">Two-factor code</div>
        <div class="otp-code">{otp}</div>
        <div class="otp-expiry">Expires in 15 minutes</div>
      </div>
      <p>If this wasn't you, change your password right away.</p>
      <hr class="divider" />
      <div class="note">
        <strong>Security tip:</strong> MinuteAI will never ask for this code outside the sign-in screen.
      </div>
    """
    resend.Emails.send({
        "from": _FROM,
        "to": [to_email],
        "subject": f"{otp} is your MinuteAI sign-in code",
        "html": _base_template("Confirm your sign-in - MinuteAI", body),
    })


def send_team_invite(
    to_email: str,
    inviter_name: str,
    invitee_name: str,
    role: str,
    has_account: bool,
    invite_token: str = "",
    workspace_name: str = "the workspace",
) -> None:
    role_label    = role.title()
    inviter_first = inviter_name.split()[0] if inviter_name else "Someone"
    invitee_first = invitee_name.split()[0] if invitee_name else "there"
    cta_url   = f"{_APP_URL}/accept-invite?token={invite_token}"
    cta_label = "Accept invitation"

    body = f"""
      <h1>You've been invited to MinuteAI</h1>
      <p>Hi {invitee_first},</p>
      <p><strong>{inviter_name}</strong> has invited you to join <strong>{workspace_name}</strong> on MinuteAI as a <strong>{role_label}</strong>.</p>
      <div style="background:linear-gradient(135deg,#f5f3ff,#eef2ff);border:1px solid #ddd6fe;border-radius:12px;padding:20px 24px;margin:24px 0;text-align:center;">
        <p style="font-size:13px;color:#6b7280;margin-bottom:6px;">Invited by</p>
        <p style="font-size:18px;font-weight:700;color:#1e1b4b;margin-bottom:8px;">{inviter_name}</p>
        <span style="display:inline-block;background:#7c3aed;color:white;font-size:11px;font-weight:600;padding:3px 12px;border-radius:99px;">{role_label}</span>
      </div>
      <p>MinuteAI automatically transcribes and summarises your meetings — saving hours of manual note-taking every week.</p>
      <div style="text-align:center;margin:28px 0;">
        <a href="{cta_url}" class="btn">{cta_label}</a>
      </div>
      <hr class="divider" />
      <div class="note">
        <strong>Not expecting this?</strong> You can safely ignore this email. You won't be added to any workspace without signing in.
      </div>
    """
    try:
        resend.Emails.send({
            "from":    _FROM,
            "to":      [to_email],
            "subject": f"{inviter_first} invited you to join MinuteAI",
            "html":    _base_template("You've been invited to MinuteAI", body),
        })
        log.info("Team invite sent to %s", to_email)
    except Exception as e:
        log.error("Failed to send team invite to %s: %s", to_email, e)


def send_password_reset_otp(to_email: str, name: str, otp: str) -> None:
    first = name.split()[0] if name else "there"
    body = f"""
      <h1>Reset your password</h1>
      <p>Hi {first}, we received a request to reset the password for your MinuteAI account.</p>
      <div class="otp-block">
        <div class="otp-label">Password reset code</div>
        <div class="otp-code">{otp}</div>
        <div class="otp-expiry">Expires in 15 minutes</div>
      </div>
      <p>Enter this code along with your new password to complete the reset.</p>
      <hr class="divider" />
      <div class="note">
        <strong>Didn't request a reset?</strong> Your password has not been changed. If you're concerned about account security, please contact us immediately.
      </div>
    """
    resend.Emails.send({
        "from": _FROM,
        "to": [to_email],
        "subject": f"{otp} is your MinuteAI password reset code",
        "html": _base_template("Reset your password — MinuteAI", body),
    })
