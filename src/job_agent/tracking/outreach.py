"""Outreach drafts: one per role, never twice to the same inbox.

The agent drafts cold emails; it never sends them. What it guarantees is that a
recipient is not written to repeatedly, whichever sweep, board or re-run finds
the role again:

* The ledger (`outreach_log` in the delta store) is keyed by recipient and role
  fingerprint. A role already drafted to an address gets its original draft
  back, marked as drafted before, and no new email.
* A recipient already drafted to about a *different* role within
  `COOLDOWN_DAYS` is put on hold rather than sent a second email that week.
* Each ready draft is also written as an `.eml` file with the tailored resume
  attached. It opens in Outlook, Thunderbird or Apple Mail as an unsent draft.

Drafts are kept in `outreach_drafts.json`, keyed by job ID, and survive new
sweeps, so re-running tracking does not spend another LLM call per job.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from job_agent.config.schema import CandidateProfile, JobPosting
from job_agent.config.settings import settings

COOLDOWN_DAYS = 14

READY = "ready"
NO_EMAIL = "no_email"
DRAFTED_BEFORE = "drafted_before"
HOLD = "hold"

STATUS_LABELS = {
    READY: "Ready to send",
    NO_EMAIL: "No email found - use the draft on LinkedIn or the careers page",
    DRAFTED_BEFORE: "Already drafted - do not send again",
    HOLD: "On hold - this address was emailed recently about another role",
}


def drafts_path() -> Path:
    return settings.outputs_dir / "outreach_drafts.json"


def load_drafts() -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(drafts_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_drafts(drafts: Dict[str, Dict[str, Any]]) -> None:
    path = drafts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(drafts, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def split_subject(text: str) -> Tuple[str, str]:
    """Separate a "Subject: ..." first line from the body."""
    lines = (text or "").strip().splitlines()
    if lines and re.match(r"^\s*subject\s*:", lines[0], re.IGNORECASE):
        subject = lines[0].split(":", 1)[1].strip()
        return subject, "\n".join(lines[1:]).strip()
    return "", (text or "").strip()


@dataclass
class OutreachPlan:
    recipient: Optional[str]
    status: str
    note: str
    previous: Optional[Dict[str, Any]] = None


def plan_outreach(store, job: JobPosting, now: Optional[datetime] = None) -> OutreachPlan:
    """Decide whether this role gets a new draft, its earlier one, or none yet."""
    contact = job.primary_contact()
    if contact is None:
        return OutreachPlan(None, NO_EMAIL, STATUS_LABELS[NO_EMAIL])
    recipient = contact.email.lower()

    earlier = store.outreach_record(recipient, job.fingerprint())
    if earlier:
        when = earlier["drafted_at"][:10]
        if earlier["job_id"] == job.id:
            note = f"Drafted on {when} - do not send again"
        else:
            note = f"Same role was already drafted to {recipient} on {when} (another listing) - do not send again"
        return OutreachPlan(recipient, DRAFTED_BEFORE, note, earlier)

    last = store.last_outreach_to(recipient)
    now = now or datetime.now(timezone.utc)
    if last:
        drafted = datetime.fromisoformat(last["drafted_at"])
        if now - drafted < timedelta(days=COOLDOWN_DAYS):
            return OutreachPlan(
                recipient, HOLD,
                f"{recipient} was drafted to on {last['drafted_at'][:10]} about \"{last['title']}\". "
                f"Wait until {(drafted + timedelta(days=COOLDOWN_DAYS)).date()} or mention both roles in one email.",
                last,
            )
    return OutreachPlan(recipient, READY, STATUS_LABELS[READY])


def write_eml(
    path: Path,
    profile: CandidateProfile,
    recipient: str,
    subject: str,
    body: str,
    attachment: Optional[Path],
) -> Path:
    """An unsent email draft with the tailored resume attached."""
    message = EmailMessage()
    message["From"] = f"{profile.contact.full_name} <{profile.contact.email}>"
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    # Outlook opens a message carrying this header as an editable, unsent draft.
    message["X-Unsent"] = "1"
    message.set_content(body)
    if attachment and attachment.is_file():
        message.add_attachment(
            attachment.read_bytes(), maintype="application", subtype="pdf",
            filename=f"{re.sub(r'[^A-Za-z0-9]+', '_', profile.contact.full_name).strip('_')}_Resume.pdf",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(message))
    return path
