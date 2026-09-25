"""Small, value-like presentations for the Stream Deck and its web mirror.

Every status key separates what was *observed* from what a press will request.
The renderer owns pixels; this module owns the words and phases.
"""

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class StatusKey:
    control: str
    observed: Optional[str]
    next_action: Optional[str]
    phase: str = "ready"
    target: Optional[str] = None

    def as_dict(self) -> dict:
        """Return a copy for both physical rendering and the LAN status API."""
        return asdict(self)


def host_status(
    source: Optional[str],
    *,
    pending: bool = False,
    pending_pc: Optional[int] = None,
    fault: bool = False,
) -> StatusKey:
    """PC 1 ↔ PC 2; a recognized Pi input routes to PC 1."""
    labels = {"pc1": "PC 1", "pc2": "PC 2", "pi": "PI"}
    observed = labels.get(source)
    next_host = "PC 2" if source == "pc1" else "PC 1"
    if pending:
        destination = f"PC {pending_pc + 1}" if pending_pc in (0, 1) else "READING"
        return StatusKey(
            "HOST",
            observed if not fault else None,
            f"→ {destination}",
            "pending",
            f"… {destination}",
        )
    if fault:
        return StatusKey("HOST", observed, None, "error")
    if not observed:
        return StatusKey("HOST", None, None, "unknown")
    return StatusKey("HOST", observed, f"→ {next_host}")


def ha_toggle_status(
    button: Mapping[str, Any],
    state: str,
    *,
    last_confirmed: Optional[str] = None,
    pending: Optional[str] = None,
    failed: bool = False,
) -> StatusKey:
    """Describe a state-observed AC or shade toggle without guessing a result."""
    entity = str(button.get("state_entity", ""))
    control = (
        "AC"
        if entity.startswith("climate.")
        else "SHADES"
        if entity.startswith("cover.")
        else "LIGHTS"
    )
    inactive = str(button.get("label") or "OFF").replace("\n", " ").strip().upper()
    active = str(button.get("active_label") or "ON").replace("\n", " ").strip().upper()
    if (
        pending
        and state in {"mixed", "moving", "other"}
        and last_confirmed in {"active", "inactive"}
    ):
        observed = active if last_confirmed == "active" else inactive
        next_state = inactive if last_confirmed == "active" else active
    elif state == "active":
        observed, next_state = active, inactive
    elif state == "inactive":
        observed, next_state = inactive, active
    elif state in {"mixed", "other"}:
        observed, next_state = ("MIXED" if state == "mixed" else "CUSTOM"), active
    elif state == "moving":
        return StatusKey(
            control,
            "MOVING",
            None if failed else "WAIT",
            "error" if failed else "blocked",
        )
    else:
        if pending:
            target = (
                active
                if pending == "active"
                else inactive
                if pending == "inactive"
                else "READING"
            )
            return StatusKey(control, None, f"→ {target}", "pending", f"… {target}")
        return StatusKey(control, None, None, "error" if failed else "unknown")
    if pending:
        target = (
            active
            if pending == "active"
            else inactive
            if pending == "inactive"
            else "READING"
        )
        return StatusKey(control, observed, f"→ {target}", "pending", f"… {target}")
    if failed:
        return StatusKey(control, observed, next_state, "error")
    return StatusKey(control, observed, f"→ {next_state}")


def keypad_status(
    label: str,
    led_state: str,
    *,
    pending: bool = False,
    acknowledged: bool = False,
    failed: bool = False,
) -> StatusKey:
    """The LED is observed independently of an accepted keypad press."""
    observed = {"on": "LED ON", "off": "LED OFF"}.get(led_state, "LED ?")
    phase = (
        "pending"
        if pending
        else "error"
        if failed
        else "ack"
        if acknowledged
        else "ready"
    )
    return StatusKey(
        label.upper().strip(), observed, "PRESS", phase, "… PRESS" if pending else None
    )
