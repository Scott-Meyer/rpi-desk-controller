"""Native MQTT settings dialogs for the desktop tray application."""

import logging
import platform
from typing import Any, Dict, Mapping, Optional

from desk_controller.config import normalize_mqtt_config

logger = logging.getLogger(__name__)


def _show_macos_error(message: str) -> None:
    import AppKit

    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_("Invalid MQTT Settings")
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_("OK")
    alert.runModal()


def _show_macos_settings(
    current: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    import AppKit

    AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    values = dict(current)

    while True:
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("MQTT Settings")
        alert.setInformativeText_(
            "Connect Desk Agent to the same MQTT broker as the desk controller."
        )
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")

        accessory = AppKit.NSView.alloc().initWithFrame_(((0, 0), (390, 146)))
        fields = {}
        definitions = [
            ("broker", "Broker", False, 112),
            ("port", "Port", False, 78),
            ("username", "Username", False, 44),
            ("password", "Password", True, 10),
        ]
        for key, label_text, secure, y_position in definitions:
            label = AppKit.NSTextField.labelWithString_(label_text)
            label.setFrame_(((0, y_position), (90, 24)))
            accessory.addSubview_(label)

            field_class = AppKit.NSSecureTextField if secure else AppKit.NSTextField
            field = field_class.alloc().initWithFrame_(((96, y_position), (294, 24)))
            field.setStringValue_(str(values.get(key, "")))
            accessory.addSubview_(field)
            fields[key] = field

        alert.setAccessoryView_(accessory)
        response = alert.runModal()
        if response != AppKit.NSAlertFirstButtonReturn:
            return None

        values = {key: field.stringValue() for key, field in fields.items()}
        try:
            return normalize_mqtt_config(values)
        except ValueError as exc:
            _show_macos_error(str(exc))


def _show_windows_settings(
    current: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    import tkinter
    from tkinter import messagebox, ttk

    root = tkinter.Tk()
    root.title("Desk Agent — MQTT Settings")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    result = None

    frame = ttk.Frame(root, padding=20)
    frame.grid(row=0, column=0, sticky="nsew")
    ttk.Label(
        frame,
        text="Connect Desk Agent to your MQTT broker",
        font=("Segoe UI", 12, "bold"),
    ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 16))

    fields = {}
    definitions = [
        ("broker", "Broker", False),
        ("port", "Port", False),
        ("username", "Username", False),
        ("password", "Password", True),
    ]
    for row, (key, label_text, secure) in enumerate(definitions, start=1):
        ttk.Label(frame, text=label_text).grid(
            row=row,
            column=0,
            sticky="w",
            padx=(0, 14),
            pady=6,
        )
        entry = ttk.Entry(
            frame,
            width=36,
            show="•" if secure else "",
        )
        entry.insert(0, str(current.get(key, "")))
        entry.grid(row=row, column=1, sticky="ew", pady=6)
        fields[key] = entry

    ttk.Label(
        frame,
        text="Credentials are stored in your Windows profile.",
        foreground="#666666",
    ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 18))

    button_row = ttk.Frame(frame)
    button_row.grid(row=6, column=0, columnspan=2, sticky="e")

    def cancel():
        root.destroy()

    def save():
        nonlocal result
        values = {key: entry.get() for key, entry in fields.items()}
        try:
            result = normalize_mqtt_config(values)
        except ValueError as exc:
            messagebox.showerror(
                "Invalid MQTT Settings",
                str(exc),
                parent=root,
            )
            return
        root.destroy()

    ttk.Button(
        button_row,
        text="Cancel",
        command=cancel,
    ).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(
        button_row,
        text="Save",
        command=save,
    ).grid(row=0, column=1)

    root.protocol("WM_DELETE_WINDOW", cancel)
    root.bind("<Escape>", lambda event: cancel())
    root.bind("<Return>", lambda event: save())
    fields["broker"].focus_set()
    root.update_idletasks()
    width = root.winfo_reqwidth()
    height = root.winfo_reqheight()
    x_position = (root.winfo_screenwidth() - width) // 2
    y_position = (root.winfo_screenheight() - height) // 3
    root.geometry(f"{width}x{height}+{x_position}+{y_position}")
    root.after(500, lambda: root.attributes("-topmost", False))
    root.mainloop()
    return result


def show_mqtt_settings(
    current: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Show the platform-native MQTT editor and return saved values."""
    system = platform.system().lower()
    if system == "darwin":
        return _show_macos_settings(current)
    if system == "windows":
        return _show_windows_settings(current)

    logger.error("MQTT settings dialog is not supported on %s", system)
    return None
