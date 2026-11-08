"""Tests for the GUI-surface ``drive_preview`` tool."""

import base64
import json

from tools import drive_preview_tool as ap




def test_requires_callback():
    """Outside the desktop GUI there is no bridge — a clear error, no crash."""
    result = json.loads(ap.drive_preview_tool(action="elements", callback=None))
    assert "desktop" in result["error"]


def test_rejects_an_unknown_action():
    result = json.loads(ap.drive_preview_tool(action="teleport", callback=lambda _p: "{}"))
    assert "action must be one of" in result["error"]


def test_interaction_verbs_need_a_target():
    """A click with nowhere to land is a mistake worth naming before the bridge."""
    for verb in ("click", "type", "press"):
        result = json.loads(ap.drive_preview_tool(action=verb, text="x", key="Enter", callback=lambda _p: "{}"))
        assert "ref" in result["error"], verb


def test_type_needs_text_and_press_needs_a_key():
    calls = []

    def cb(payload):
        calls.append(payload)
        return json.dumps({"success": True})

    assert "text" in json.loads(ap.drive_preview_tool(action="type", ref="@e1", callback=cb))["error"]
    assert "key" in json.loads(ap.drive_preview_tool(action="press", ref="@e1", callback=cb))["error"]
    assert calls == []


def test_typing_an_empty_string_is_allowed():
    """Clearing a field is a real intent — only a missing `text` is an error."""
    seen = {}
    ap.drive_preview_tool(action="type", ref="@e1", text="", callback=lambda p: seen.update(p) or "{}")

    assert seen["text"] == ""


def test_scroll_needs_no_target_and_validates_its_destination():
    seen = {}

    def cb(payload):
        seen.clear()
        seen.update(payload)
        return json.dumps({"success": True})

    ap.drive_preview_tool(action="scroll", callback=cb)
    assert seen == {"action": "scroll"}

    result = json.loads(ap.drive_preview_tool(action="scroll", to="sideways", callback=cb))
    assert "to must be one of" in result["error"]


def test_payload_forwards_only_what_was_given():
    seen = {}
    ap.drive_preview_tool(
        action="type",
        ref="inp-password",
        text="hunter2",
        submit=True,
        callback=lambda p: seen.update(p) or json.dumps({"success": True}),
    )

    assert seen == {"action": "type", "ref": "inp-password", "text": "hunter2", "submit": True}




def test_numeric_arguments_are_validated():
    result = json.loads(ap.drive_preview_tool(action="scroll", amount="lots", callback=lambda _p: "{}"))
    assert "integers" in result["error"]


def test_empty_answer_means_nothing_open():
    result = json.loads(ap.drive_preview_tool(action="elements", callback=lambda _p: ""))
    assert "open_preview" in result["error"]


def test_passes_the_renderer_answer_through():
    payload = {
        "success": True,
        "acted": 'clicked button "Sign in"',
        "url": "https://example.com/app",
        "elements": [{"ref": "@e1", "role": "button", "label": "Log out", "selector": "#out"}],
    }
    result = json.loads(ap.drive_preview_tool(action="click", ref="@e2", callback=lambda _p: json.dumps(payload)))

    assert result == payload


def test_wraps_non_json_text():
    result = json.loads(ap.drive_preview_tool(action="elements", callback=lambda _p: "plain words"))
    assert result == {"text": "plain words"}


def test_callback_failure_is_reported():
    def _boom(_payload):
        raise RuntimeError("renderer went away")

    result = json.loads(ap.drive_preview_tool(action="elements", callback=_boom))
    assert "renderer went away" in result["error"]


def test_empty_answer_distinguishes_no_tab_from_a_stale_app():
    """An empty bridge answer used to be one merged "timed out, or no window
    answered" string that blamed a closed tab even when the pane was open on an
    app older than this backend (#94272): the two cases need different next
    steps (open a tab vs update the app)."""
    result = json.loads(ap.drive_preview_tool(action="elements", callback=lambda _p: ""))

    assert "no preview tab is open" in result["error"]
    assert "older than this backend" in result["error"]


def test_setfiles_packs_the_file_into_the_payload(tmp_path):
    """setfiles embeds the bytes itself — the page never fetches anything, so
    the payload carries exactly the setfiles fields even when arguments meant
    for other verbs are set."""
    data = b"%PDF-1.4 minimal"
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(data)
    seen = {}
    ap.drive_preview_tool(
        action="setfiles",
        selector="input[type=file]",
        file=str(pdf),
        text="junk another verb might set",
        submit=True,
        callback=lambda p: seen.update(p) or json.dumps({"success": True}),
    )

    assert seen == {
        "action": "setfiles",
        "selector": "input[type=file]",
        "file_name": "report.pdf",
        "file_b64": base64.b64encode(data).decode("ascii"),
        "file_mime": "application/pdf",
    }


def test_setfiles_needs_a_file_path():
    result = json.loads(ap.drive_preview_tool(
        action="setfiles", selector="input[type=file]", callback=lambda _p: "{}"))
    assert "needs file" in result["error"]


def test_setfiles_missing_file_names_the_path(tmp_path):
    missing = tmp_path / "nope.pdf"
    calls = []
    result = json.loads(ap.drive_preview_tool(
        action="setfiles", selector="input[type=file]", file=str(missing),
        callback=lambda p: calls.append(p) or "{}"))
    assert str(missing) in result["error"]
    assert calls == []


def test_setfiles_rejects_files_over_10_mib(tmp_path, monkeypatch):
    """A tiny real file stands in for a huge one; only the reported size is
    patched, so the test needs neither 10 MiB on disk nor a sparse file."""
    chunky = tmp_path / "chunky.pdf"
    chunky.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(ap.os.path, "getsize", lambda _p: ap.MAX_FILE_BYTES + 1)
    result = json.loads(ap.drive_preview_tool(
        action="setfiles", selector="input[type=file]", file=str(chunky), callback=lambda _p: "{}"))
    assert "10 MiB" in result["error"]


def test_setfiles_rejects_refs(tmp_path):
    """A ref from the inventory can't name a file input — only a selector can."""
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    result = json.loads(ap.drive_preview_tool(
        action="setfiles", ref="inp-upload", file=str(pdf), callback=lambda _p: "{}"))
    assert "refs are not supported" in result["error"]


def test_setfiles_unknown_suffix_defaults_to_octet_stream(tmp_path):
    blob = tmp_path / "export.dat"
    blob.write_bytes(b"\x00\x01\x02")
    seen = {}
    ap.drive_preview_tool(
        action="setfiles", selector="input[type=file]", file=str(blob),
        callback=lambda p: seen.update(p) or json.dumps({"success": True}))
    assert seen["file_mime"] == "application/octet-stream"


def test_setfiles_forwards_mime_override_and_verify_selector(tmp_path):
    pdf = tmp_path / "table.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    seen = {}
    ap.drive_preview_tool(
        action="setfiles", selector="input[type=file]", file=str(pdf), mime="text/csv",
        verify_selector="#dropzone",
        callback=lambda p: seen.update(p) or json.dumps({"success": True}))
    assert seen["file_mime"] == "text/csv"
    assert seen["verify_selector"] == "#dropzone"
