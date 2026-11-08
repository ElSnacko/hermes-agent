#!/usr/bin/env python3
"""Interact with the in-app browser / preview pane in the Hermes desktop GUI (click, type, scroll).

Elements are addressed by legible refs from ``action="elements"`` (``btn-sign-in``); a ref survives
re-renders and only a navigation retires it, so the renderer answers with a *delta* instead of
re-sending the inventory. Round-trips through the gateway's blocking-prompt bridge
(``preview.act.request`` / ``preview.act.respond``); this module is schema + a thin dispatcher over
the platform-injected callback. ``desktop_ui`` toolset: desktop-sourced sessions only.
"""

import base64
import os
from typing import Callable, Optional

from tools.desktop_ui import passthrough_json
from tools.browser_vault_preview import redact_preview_result
from tools.registry import registry, tool_error

ACTIONS = ("elements", "click", "hover", "type", "scroll", "press", "strobe", "back", "forward", "reload",
           "setfiles")
SCROLL_TO = ("top", "bottom")

# One setfiles embeds the whole file in a single act payload (the renderer
# rebuilds it in-page). The act request frame rides a bounded ~4 MiB per-peer
# mailbox and base64 inflates the bytes by 4/3, leaving ~3 MiB before frame
# overhead — 2 MiB is the safe ceiling.
MAX_FILE_BYTES = 2 * 1024 * 1024
FILE_MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": ("application/vnd.openxmlformats-officedocument."
              "wordprocessingml.document"),
    ".txt": "text/plain",
    ".rtf": "application/rtf",
}

# Verbs that need something to act on — a ref from the last inventory, or a
# raw CSS selector. `scroll` is deliberately absent: bare, it scrolls the page.
# So is `setfiles`: it is selector-only (a ref can't name a file input), and its
# own branch refuses a missing selector with a message that says so instead of
# pointing the caller at a ref it cannot use.
NEEDS_TARGET = ("click", "hover", "type", "press")


def _setfiles_fields(selector, file, mime, verify_selector):
    """Validate one setfiles call and package the file for the renderer.

    The bytes travel inside the act payload — the page never fetches anything;
    the renderer rebuilds a File from them and drops it on the input via
    DataTransfer. Returns ``(fields, None)`` or ``(None, tool_error)``.
    """
    if not selector:
        return None, tool_error(
            "setfiles needs a CSS selector for the file input (refs are not supported; "
            "run action='elements' and use the field's selector id).")
    if not file:
        return None, tool_error("setfiles needs file: the local path of the file to attach.")
    if not os.path.isfile(file):
        return None, tool_error(f"setfiles file does not exist or is not a regular file: {file}")
    # getsize and read inside one guard: the file can change shape between the
    # two, and an OSError from either (vanished file, permissions) is a tool
    # error naming the path, never a traceback out of the tool.
    try:
        size = os.path.getsize(file)
        if size == 0:
            return None, tool_error(f"setfiles cannot attach {file}: the file is empty.")
        if size > MAX_FILE_BYTES:
            return None, tool_error(
                f"setfiles file is {size} bytes; the limit is 2 MiB "
                f"(the preview bridge caps request payloads): {file}")
        with open(file, "rb") as fh:
            file_b64 = base64.b64encode(fh.read()).decode("ascii")
    except OSError as exc:
        return None, tool_error(f"setfiles cannot read {file}: {exc}")
    return (
        ("action", "setfiles"), ("selector", selector), ("file_name", os.path.basename(file)),
        ("file_b64", file_b64),
        ("file_mime", mime or FILE_MIME_BY_SUFFIX.get(
            os.path.splitext(file)[1].lower(), "application/octet-stream")),
        ("verify_selector", verify_selector),
    ), None


def drive_preview_tool(
    action: str = "", ref: Optional[str] = None, selector: Optional[str] = None, text: Optional[str] = None,
    key: Optional[str] = None, submit: Optional[bool] = None, amount: Optional[int] = None,
    to: Optional[str] = None, limit: Optional[int] = None, full: Optional[bool] = None,
    allow_shortcut: Optional[bool] = None, file: Optional[str] = None, mime: Optional[str] = None,
    verify_selector: Optional[str] = None,
    callback: Optional[Callable] = None) -> str:
    """Dispatch one interaction to the desktop renderer and return its outcome."""
    if callback is None:
        return tool_error("drive_preview is only available in the Hermes desktop app.")
    verb = (action or "").strip().lower()
    if verb not in ACTIONS:
        return tool_error(f"action must be one of: {', '.join(ACTIONS)}.")
    if verb in NEEDS_TARGET and not (ref or selector):
        return tool_error(f"{verb} needs a ref from action='elements' (e.g. 'btn-sign-in') or a CSS selector.")
    if verb == "type" and text is None:
        return tool_error("type needs the text to enter.")
    if verb == "press" and not key:
        return tool_error("press needs a key, e.g. 'Enter' or 'Escape'.")
    if to is not None and to not in SCROLL_TO:
        return tool_error(f"to must be one of: {', '.join(SCROLL_TO)}.")
    if verb == "setfiles":
        fields, err = _setfiles_fields(selector, file, mime, verify_selector)
        if err:
            return err
    else:
        try:
            fields = (
                ("action", verb), ("ref", ref), ("selector", selector), ("text", text), ("key", key),
                ("submit", submit), ("full", full), ("to", to), ("allow_shortcut", allow_shortcut),
                ("amount", None if amount is None else int(amount)), ("max", None if limit is None else int(limit)),
            )
        except (TypeError, ValueError):
            return tool_error("amount and max must be integers.")
    try:
        raw = callback({name: val for name, val in fields if val is not None})
    except Exception as exc:
        return redact_preview_result(tool_error(f"Failed to act on the in-app browser: {exc}"))
    if not raw:
        return tool_error(
            "No GUI window answered with a page: no preview tab is open. "
            "Open a page with open_preview first. If the pane IS open, the desktop app "
            "may be older than this backend — its bridge-unavailable error names that case.")
    return redact_preview_result(passthrough_json(raw))


ACT_PREVIEW_SCHEMA = {
    "name": "drive_preview",
    # Response-shape teaching kept only where skipping it wastes calls (delta
    # semantics, rebound refs, strobe's burst): a model that doesn't know them
    # re-reads pages or loops strobe.
    # See #95681.
    "description": (
        "Use the desktop preview-pane page: log in, fill forms, click through flows. "
        "It is the page `desktop_preview` opens. ALWAYS "
        "start with action='elements' — it inventories clickable/typable "
        "things as refs ('btn-sign-in') with role/label/value; act by ref, "
        "not guessed selectors. Refs survive re-renders and only die on "
        "navigation (you'll be told they're stale — call elements again). "
        "After the first full inventory, actions answer with a DELTA: "
        "'added' in full, 'changed' as ref + moved fields, 'removed'/"
        "'rebound' as ref lists ('rebound' needs nothing from you — the ref "
        "already follows the rebuilt element). Anything unmentioned is "
        "unchanged; do not re-read to check. Input is real (pointer travels, "
        "hover menus open). Actions: elements, click, hover (park the "
        "pointer — opens dropdowns before clicking in), type (submit=true "
        "also presses Enter), scroll, press, strobe (visual flourish only — "
        "one call runs a multi-second burst; never loop it), back/forward/"
        "reload. setfiles attaches a local file to a file input (file=PATH "
        "plus selector; the file bytes are embedded, not fetched by the page). "
        "Moves draw live and fade; annotate_preview leaves a lasting "
        "mark. A printable press on body/html is refused unless allow_shortcut "
        "is true. Page text only: desktop_preview action=read. Separate automated "
        "browser: browser_* tools."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(ACTIONS),
                "description": "Start with 'elements'.",
            },
            "ref": {
                "type": "string",
                "description": "Element ref from an earlier elements call.",
            },
            "selector": {
                "type": "string",
                "description": "CSS selector fallback. Prefer ref.",
            },
            "text": {"type": "string", "description": "type: the text."},
            "submit": {
                "type": "boolean",
                "description": "type: press Enter + submit the form after.",
            },
            "key": {
                "type": "string",
                "description": "press: key name ('Enter', 'Escape', 'ArrowDown').",
            },
            "allow_shortcut": {
                "type": "boolean",
                "description": "press: allow a printable key on body/html. Off by default.",
            },
            "amount": {
                "type": "integer",
                "description": "scroll: pixels (negative = up; default ~one screen).",
            },
            "to": {
                "type": "string",
                "enum": list(SCROLL_TO),
                "description": "scroll: jump to top/bottom instead.",
            },
            "max": {
                "type": "integer",
                "description": "elements: cap the inventory.",
            },
            "full": {
                "type": "boolean",
                "description": "elements: full re-read instead of a delta. Rarely needed.",
            },
            "file": {"type": "string", "description": "setfiles: local path of the file to attach (max 2 MiB)."},
            "mime": {"type": "string", "description": "setfiles: MIME override; default is guessed from the suffix."},
            "verify_selector": {
                "type": "string",
                "description": "setfiles: selector to confirm after the attach, "
                               "whose rendered text should name the file.",
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="drive_preview",
    toolset="desktop_ui",
    schema=ACT_PREVIEW_SCHEMA,
    handler=lambda args, **kw: drive_preview_tool(
        action=args.get("action", ""), limit=args.get("max"), callback=kw.get("callback"),
        **{k: args.get(k) for k in (
            "ref", "selector", "text", "key", "submit", "amount", "to", "full", "allow_shortcut",
            "file", "mime", "verify_selector")},
    ),
    emoji="🖱️")
