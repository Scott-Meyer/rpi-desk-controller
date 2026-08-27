"""Native MQTT settings dialogs for the desktop tray application."""

import logging
import platform
import threading
from typing import Any, Dict, Mapping, Optional

from desk_controller.config import normalize_mqtt_config
from desk_controller.core.mqtt_client import test_mqtt_connection

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
    status_info: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    import AppKit

    AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    values = dict(current)

    while True:
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("MQTT Settings")
        info_text = "Connect Desk Agent to the same MQTT broker as the desk controller."
        if status_info:
            if status_info.get("auth_failed"):
                info_text += f"\n\nStatus: Authentication failed ({status_info.get('auth_error', 'bad credentials')})."
            elif status_info.get("connected"):
                info_text += "\n\nStatus: Connected."
            else:
                info_text += "\n\nStatus: Offline."
        alert.setInformativeText_(info_text)
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
            normalized = normalize_mqtt_config(values)
        except ValueError as exc:
            _show_macos_error(str(exc))
            continue

        # Test credentials before saving
        success, message = test_mqtt_connection(
            broker=normalized["broker"],
            port=normalized["port"],
            username=normalized.get("username"),
            password=normalized.get("password"),
            timeout=4.0,
        )
        if not success and "Authentication failed" in message:
            _show_macos_error(
                f"Could not connect to MQTT broker:\n\n{message}\n\nPlease check your username and password."
            )
            continue

        return normalized


def _show_windows_settings(
    current: Mapping[str, Any],
    status_info: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    import ctypes
    import tkinter
    from tkinter import ttk

    # Ensure this thread receives keyboard and mouse input on Windows
    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        cur_tid = kernel32.GetCurrentThreadId()
        fg_hwnd = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None)
        if fg_tid and fg_tid != cur_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, True)
    except Exception:
        pass

    root = tkinter.Tk()
    root.title("Desk Agent — MQTT Settings")
    root.resizable(False, False)
    result = None

    frame = ttk.Frame(root, padding=20)
    frame.grid(row=0, column=0, sticky="nsew")
    ttk.Label(
        frame,
        text="Connect Desk Agent to your MQTT broker",
        font=("Segoe UI", 12, "bold"),
    ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

    # Initial status display
    status_frame = ttk.Frame(frame)
    status_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 12))

    status_var = tkinter.StringVar()
    status_color_var = tkinter.StringVar()

    if status_info and status_info.get("auth_failed"):
        status_var.set(f"● {status_info.get('auth_error', 'Authentication failed (bad credentials)')}")
        status_color_var.set("#d9383a")
    elif status_info and status_info.get("connected"):
        status_var.set("● Currently connected to MQTT broker")
        status_color_var.set("#2ea043")
    elif status_info and not status_info.get("connected"):
        status_var.set("● Currently offline / disconnected")
        status_color_var.set("#e36209")
    else:
        status_var.set("● Enter broker details and test or save connection")
        status_color_var.set("#57606a")

    status_label = tkinter.Label(
        status_frame,
        textvariable=status_var,
        fg=status_color_var.get(),
        font=("Segoe UI", 9, "bold"),
        anchor="w",
        wraplength=380,
        justify="left",
    )
    status_label.pack(fill="x")

    def update_status(text: str, color: str):
        status_var.set(text)
        status_label.config(fg=color)

    fields = {}
    definitions = [
        ("broker", "Broker", False),
        ("port", "Port", False),
        ("username", "Username", False),
        ("password", "Password", True),
    ]
    for row, (key, label_text, secure) in enumerate(definitions, start=2):
        ttk.Label(frame, text=label_text).grid(
            row=row,
            column=0,
            sticky="w",
            padx=(0, 14),
            pady=5,
        )
        val = current.get(key, "")
        if val is None:
            val = ""
        entry = tkinter.Entry(
            frame,
            width=32,
            show="•" if secure else "",
            font=("Segoe UI", 9),
            bg="#ffffff",
            fg="#000000",
            insertbackground="#000000",
            relief="solid",
            bd=1,
        )
        entry.insert(0, str(val))
        entry.grid(row=row, column=1, sticky="ew", pady=5, ipady=3)
        fields[key] = entry

    ttk.Label(
        frame,
        text="Credentials are stored in your Windows user profile.",
        foreground="#666666",
        font=("Segoe UI", 8),
    ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 14))

    button_row = ttk.Frame(frame)
    button_row.grid(row=7, column=0, columnspan=2, sticky="ew")
    button_row.columnconfigure(0, weight=1)

    left_buttons = ttk.Frame(button_row)
    left_buttons.grid(row=0, column=0, sticky="w")

    right_buttons = ttk.Frame(button_row)
    right_buttons.grid(row=0, column=1, sticky="e")

    is_testing = False

    def get_normalized():
        values = {key: entry.get() for key, entry in fields.items()}
        return normalize_mqtt_config(values)

    def cancel():
        root.destroy()

    def run_test(on_done=None):
        nonlocal is_testing
        if is_testing:
            return
        try:
            cfg = get_normalized()
        except ValueError as exc:
            update_status(f"✗ {exc}", "#d9383a")
            if on_done:
                on_done(False, str(exc), None)
            return

        is_testing = True
        test_btn.config(state="disabled")
        save_btn.config(state="disabled")
        update_status("● Testing connection to MQTT broker…", "#0969da")

        def worker():
            success, msg = test_mqtt_connection(
                broker=cfg["broker"],
                port=cfg["port"],
                username=cfg.get("username"),
                password=cfg.get("password"),
                timeout=4.0,
            )

            def on_complete():
                nonlocal is_testing
                is_testing = False
                test_btn.config(state="normal")
                save_btn.config(state="normal")
                if success:
                    update_status(f"✓ {msg}", "#2ea043")
                elif "Authentication failed" in msg:
                    update_status(f"✗ {msg}", "#d9383a")
                else:
                    update_status(f"✗ {msg}", "#e36209")

                if on_done:
                    on_done(success, msg, cfg)

            root.after(0, on_complete)

        threading.Thread(target=worker, daemon=True, name="mqtt-settings-test").start()

    def on_test_click():
        run_test()

    def on_save_click():
        nonlocal result
        try:
            get_normalized()
        except ValueError as exc:
            update_status(f"✗ {exc}", "#d9383a")
            return

        def handle_save_test_result(success: bool, msg: str, tested_cfg):
            nonlocal result
            if tested_cfg is None:
                return

            if success:
                result = tested_cfg
                root.destroy()
                return

            if "Authentication failed" in msg:
                update_status(f"✗ {msg}. Check username and password.", "#d9383a")
                fields["username"].focus_set()
                return

            # If network error / timeout, allow saving
            update_status(f"✗ {msg}. (Click Save again to save anyway)", "#e36209")
            save_btn.config(command=lambda: finish_save(tested_cfg))

        def finish_save(final_cfg):
            nonlocal result
            result = final_cfg
            root.destroy()

        run_test(on_done=handle_save_test_result)

    test_btn = ttk.Button(
        left_buttons,
        text="Test Connection",
        command=on_test_click,
    )
    test_btn.pack(side="left")

    cancel_btn = ttk.Button(
        right_buttons,
        text="Cancel",
        command=cancel,
    )
    cancel_btn.pack(side="left", padx=(0, 8))

    save_btn = ttk.Button(
        right_buttons,
        text="Save",
        command=on_save_click,
    )
    save_btn.pack(side="left")

    root.protocol("WM_DELETE_WINDOW", cancel)
    root.bind("<Escape>", lambda event: cancel())
    root.update_idletasks()
    width = max(440, root.winfo_reqwidth())
    height = root.winfo_reqheight()
    x_position = (root.winfo_screenwidth() - width) // 2
    y_position = (root.winfo_screenheight() - height) // 3
    root.geometry(f"{width}x{height}+{x_position}+{y_position}")
    root.lift()
    root.focus_force()
    root.after(100, lambda: (fields["broker"].focus_set(), root.focus_force()))
    root.mainloop()
    return result


def show_mqtt_settings(
    current: Mapping[str, Any],
    status_info: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Show the platform-native MQTT editor and return saved values."""
    system = platform.system().lower()
    if system == "darwin":
        return _show_macos_settings(current, status_info=status_info)
    if system == "windows":
        return _show_windows_settings(current, status_info=status_info)

    logger.error("MQTT settings dialog is not supported on %s", system)
    return None
