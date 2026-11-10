"""Trusted desktop callbacks: URL actions, script approval and the script interpreter."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

from .strings import text


def open_url(url):
    """Open an assigned web address in the user's default browser."""
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password
            or any(c.isspace() or ord(c) < 32 for c in url)):
        raise ValueError("Only HTTP(S) URLs without credentials are allowed")
    parsed.port  # Reject malformed ports before handing the URL to Windows.
    # The shell's http(s) association is the browser chosen in Windows default apps;
    # only http(s) reaches it, so no other protocol handler or file can be started.
    os.startfile(url)


def script_interpreter():
    if not getattr(sys, "frozen", False):
        return sys.executable
    candidate = shutil.which("python")
    if not candidate:
        return None
    path = Path(candidate).resolve()
    if (path == Path(sys.executable).resolve() or path.name.lower() != "python.exe"
            or "windowsapps" in {p.lower() for p in path.parts}):
        return None
    try:
        result = subprocess.run(
            [str(path), "-I", "-c", "import json,sys;print(json.dumps(list(sys.version_info[:2])))"],
            capture_output=True, text=True, timeout=5, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        version = json.loads(result.stdout)
        if (not isinstance(version, list) or len(version) != 2
                or any(type(v) is not int for v in version) or tuple(version) < (3, 11)):
            return None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    return str(path)


def request_script_approval(window, review, language="en"):
    """Show full script content in an owned native dialog, outside browser control."""
    from System import Action
    from System.Drawing import Size
    from System.Windows.Forms import (
        Button, DialogResult, DockStyle, FlowLayoutPanel, Form, FormStartPosition,
        RichTextBox, ScrollBars, TextBox,
    )

    owner = getattr(window, "native", None)  # no window: the app is in the tray
    if owner is None or owner.IsDisposed:
        return False
    accepted = [False]

    def show():
        dialog = Form()
        dialog.Text = text(language, "script.title")
        dialog.Size = Size(900, 720)
        dialog.StartPosition = FormStartPosition.CenterParent
        header = TextBox()
        header.Multiline = True
        header.ReadOnly = True
        header.Dock = DockStyle.Top
        header.Height = 88
        header.ScrollBars = ScrollBars.Both
        header.Text = text(language, "script.header", path=review["path"], sha256=review["sha256"])
        content = RichTextBox()
        content.ReadOnly = True
        content.DetectUrls = False
        content.WordWrap = False
        content.Dock = DockStyle.Fill
        content.Text = review["content"]
        buttons = FlowLayoutPanel()
        buttons.Dock = DockStyle.Bottom
        buttons.Height = 48
        deny = Button()
        deny.Text = text(language, "script.cancel")
        deny.DialogResult = DialogResult.Cancel
        allow = Button()
        allow.Text = text(language, "script.approve")
        allow.Width = 160
        allow.DialogResult = DialogResult.OK
        buttons.Controls.Add(deny)
        buttons.Controls.Add(allow)
        dialog.Controls.Add(content)
        dialog.Controls.Add(header)
        dialog.Controls.Add(buttons)
        dialog.AcceptButton = deny
        dialog.CancelButton = deny
        try:
            accepted[0] = dialog.ShowDialog(owner) == DialogResult.OK
        finally:
            dialog.Dispose()

    if owner.InvokeRequired:
        owner.Invoke(Action(show))
    else:
        show()
    return accepted[0]
