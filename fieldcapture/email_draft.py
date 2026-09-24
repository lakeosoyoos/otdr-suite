"""
Email draft for the Field Capture page
======================================

A web page cannot attach a file to an email.  On the phone the share sheet
does it; on a PC the page hands the saved FQA to this module, which writes an
email draft (.eml) with the workbook attached and opens it with the PC's mail
program.

The draft carries ``X-Unsent: 1``.  Outlook opens an .eml with that header as a
new message ready to send (To, subject, body and attachment filled in) rather
than as a received message.  Other mail programs show it as a message with the
attachment, which can still be forwarded.  The .eml is written next to the
saved FQA either way, so the tech has it even if nothing opens.

Robert, 2026-09-23.
"""
from __future__ import annotations

import os
import subprocess
import sys
from email import policy
from email.message import EmailMessage
from pathlib import Path

# The MIME types Outlook shows with the right icon and opens in Excel.
_TYPES = {
    '.xlsm': ('application', 'vnd.ms-excel.sheet.macroEnabled.12'),
    '.xlsx': ('application', 'vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
}


def _one_line(text: str, limit: int = 300) -> str:
    """Header values: one line, no control characters (a newline in a header
    would let text choose extra headers)."""
    cleaned = ''.join(ch if ch.isprintable() else ' ' for ch in str(text or ''))
    return ' '.join(cleaned.split())[:limit]


def build_draft(attachment: str | os.PathLike, to: str, subject: str, body: str) -> bytes:
    """The bytes of an unsent email with `attachment` attached."""
    path = Path(attachment)
    maintype, subtype = _TYPES.get(path.suffix.lower(), ('application', 'octet-stream'))
    msg = EmailMessage(policy=policy.SMTP)
    to = _one_line(to).replace(';', ',')
    if to:
        msg['To'] = to
    msg['Subject'] = _one_line(subject) or path.stem
    msg['X-Unsent'] = '1'
    msg.set_content(str(body or '').replace('\r\n', '\n'))
    msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype,
                       filename=path.name)
    return msg.as_bytes()


def write_draft(attachment: str | os.PathLike, to: str, subject: str, body: str) -> Path:
    """Write the draft next to the attachment, same name with .eml."""
    path = Path(attachment)
    eml = path.with_suffix('.eml')
    eml.write_bytes(build_draft(path, to, subject, body))
    return eml


def open_with_default_app(path: str | os.PathLike) -> tuple[bool, str]:
    """Open a file the way a double-click would.  Returns (opened, error)."""
    try:
        if sys.platform.startswith('win'):
            os.startfile(str(path))                    # noqa: S606 (a file we wrote)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', str(path)])
        else:
            subprocess.Popen(['xdg-open', str(path)])
        return True, ''
    except Exception as exc:                           # no mail program, no handler
        return False, str(exc)


def reveal(path: str | os.PathLike) -> tuple[bool, str]:
    """Show a file in Explorer / Finder."""
    try:
        if sys.platform.startswith('win'):
            subprocess.Popen(['explorer', '/select,', str(path)])
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', '-R', str(path)])
        else:
            subprocess.Popen(['xdg-open', str(Path(path).parent)])
        return True, ''
    except Exception as exc:
        return False, str(exc)
