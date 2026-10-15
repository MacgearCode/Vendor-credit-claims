"""Account emails — invites and password resets. Same arrangement as 3PL Portal's
app/notify.py, and the SAME n8n workflow (3PL Portal/n8n/3pl_account_email.json): the app
composes the subject and plain-text body and POSTs them to the n8n webhook, which sends
from noreply@macgeargroup.com via Graph app-only. The app holds no mail credentials.
Payload contract: 3PL Portal/docs/email_delivery.md ("The payload contract") — the `app`
field is what lets one webhook serve both sites.

N8N_RESET_WEBHOOK_URL unset (local dev) = nothing is sent and the whole email is logged,
so a local run works offline and the copy can still be read.

Body is PLAIN TEXT, so the admin's personal note needs no escaping.
"""
import json
import logging
import os
import urllib.request

log = logging.getLogger("reporting.notify")

INVITE = "invite"
RESET = "reset"

# Read at import time: set them in the environment before the app starts.
WEBHOOK_URL = os.environ.get("N8N_RESET_WEBHOOK_URL", "")
WEBHOOK_TOKEN = os.environ.get("N8N_WEBHOOK_TOKEN", "")

SITE_NAME = "Macgear Reporting"


def in_words(minutes: int) -> str:
    """Expiry as copy, not a number — the recipient reads this."""
    if minutes >= 2880:
        return f"{round(minutes / 1440)} days"
    if minutes >= 1440:
        return "24 hours"
    if minutes >= 120:
        return f"{round(minutes / 60)} hours"
    return f"{minutes} minutes"


def compose(*, purpose: str, url: str, note: str, invited_by: str,
            expires_in: str, vendor: bool = False) -> tuple[str, str]:
    """(subject, plain-text body)."""
    if purpose == INVITE:
        subject = f"Your access to {SITE_NAME}"
        what = ("where you can see weekly sell-through, stock and trends for your brands"
                if vendor else
                "Macgear's reporting site — sales, sell-through, stock and our internal tools")
        lines = [f"You've been given access to {SITE_NAME}, {what}."]
        if note:
            lines += ["", f"{invited_by or 'Macgear'} added a note:", "", f"    {note}"]
        lines += ["", "Open this link to choose your password and sign in:", "", f"    {url}", "",
                  f"The link works once and expires in {expires_in}. You choose the password"
                  " yourself — nobody at Macgear can see it."]
    else:
        subject = f"Reset your {SITE_NAME} password"
        lines = [f"Someone asked to reset the {SITE_NAME} password for this address."]
        if note:
            lines += ["", f"{invited_by or 'Macgear'} added a note:", "", f"    {note}"]
        lines += ["", "Open this link to choose a new password:", "", f"    {url}", "",
                  f"The link works once and expires in {expires_in}. If you didn't ask for this"
                  " you can ignore this email — your current password still works."]
    lines += ["", f"Sent by {invited_by}, Macgear Group." if invited_by else "Macgear Group."]
    return subject, "\n".join(lines)


def _post(url: str, body: bytes, headers: dict) -> int:
    """The only socket in this module — the seam the tests stub."""
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
        return resp.status


def send_account_link(*, to: str, url: str, purpose: str = RESET, note: str = "",
                      invited_by: str = "", expires_min: int = 45, vendor: bool = False) -> bool:
    """Compose and hand to n8n. True = the webhook ACCEPTED it (not proof of delivery), so
    callers keep showing the copyable link either way. Never raises: a mail failure must
    not 500 an admin form, nor reveal through /forgot whether an address exists."""
    expires_in = in_words(expires_min)
    subject, body = compose(purpose=purpose, url=url, note=note, invited_by=invited_by,
                            expires_in=expires_in, vendor=vendor)
    if not WEBHOOK_URL:
        log.warning("N8N_RESET_WEBHOOK_URL unset — %s email for %s not sent.\nSubject: %s\n%s",
                    purpose, to, subject, body)
        return False
    payload = {"app": "reporting", "purpose": purpose, "to": to, "subject": subject,
               "body": body, "url": url, "note": note, "invited_by": invited_by,
               "reply_to": invited_by, "expires_in": expires_in}
    headers = {"Content-Type": "application/json"}
    if WEBHOOK_TOKEN:
        headers["X-Sync-Token"] = WEBHOOK_TOKEN
    try:
        status = _post(WEBHOOK_URL, json.dumps(payload).encode(), headers)
    except Exception as e:  # noqa: BLE001 — log and swallow, see docstring
        log.error("%s email webhook failed for %s: %s", purpose, to, e)
        return False
    log.info("%s email handed to n8n for %s (HTTP %s)", purpose, to, status)
    return True
