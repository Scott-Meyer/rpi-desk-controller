"""Loopback-only web editor for workstation-owned Stream Deck buttons."""

import json
import logging
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlsplit

import yaml
from pydantic import ValidationError

from desk_controller.config import save_config
from desk_controller.desktop_agent.kvm_hardware import DesktopKVMSettings
from desk_controller.desktop_agent.workstation_buttons import (
    DesktopWorkstationButtonRegistry,
)

logger = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 64 * 1024


class ButtonConfigurationError(ValueError):
    """A user-correctable button configuration error."""

    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def _load_existing_config(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ButtonConfigurationError(
            f"Could not read the desktop configuration: {exc}",
            HTTPStatus.INTERNAL_SERVER_ERROR,
        ) from exc
    if not isinstance(loaded, dict):
        raise ButtonConfigurationError(
            "The desktop configuration must contain a YAML mapping.",
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )
    return loaded


class DesktopButtonConfigServer:
    """Serve and apply a local agent's workstation-button configuration."""

    def __init__(
        self,
        agent,
        config_path: Path,
        host: str = "127.0.0.1",
        port: int = 8765,
    ):
        self.agent = agent
        self.config_path = Path(config_path)
        self.host = host
        self.requested_port = port
        self.api_token = secrets.token_urlsafe(32)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        port = (
            self._httpd.server_address[1]
            if self._httpd is not None
            else self.requested_port
        )
        return f"http://{self.host}:{port}/"

    def update_config_path(self, path: Path) -> None:
        self.config_path = Path(path)

    def configuration(self) -> Dict[str, Any]:
        return self.agent.button_configuration_snapshot()

    def update_configuration(self, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ButtonConfigurationError("Request body must be a JSON object.")
        raw_slots = payload.get("slots")
        if not isinstance(raw_slots, list) or len(raw_slots) > 15:
            raise ButtonConfigurationError(
                "slots must be a list containing at most 15 buttons."
            )

        snapshot = self.configuration()
        available = self.agent.available_workstation_slot_ids()
        current_slot_ids = {
            str(slot.get("slot_id", "")).strip()
            for slot in snapshot.get("slots", [])
            if str(slot.get("slot_id", "")).strip()
        }
        # Keep local assignments dormant when the Pi temporarily claims a key.
        # They become active again if that physical key is cleared later.
        allowed_slots = available | current_slot_ids

        slot_config: Dict[str, Dict[str, Any]] = {}
        for raw_slot in raw_slots:
            if not isinstance(raw_slot, dict):
                raise ButtonConfigurationError(
                    "Every configured slot must be a JSON object."
                )
            values = dict(raw_slot)
            slot_id = str(values.pop("slot_id", "")).strip()
            if not slot_id:
                raise ButtonConfigurationError("Every button needs a slot ID.")
            if slot_id in slot_config:
                raise ButtonConfigurationError(
                    f"Slot {slot_id} is configured more than once."
                )
            if slot_id not in allowed_slots:
                raise ButtonConfigurationError(
                    (
                        f"Slot {slot_id} is not advertised by the Pi."
                        if available
                        else (
                            f"Slot {slot_id} cannot be added until the Pi "
                            "advertises its layout."
                        )
                    ),
                    HTTPStatus.CONFLICT,
                )
            slot_config[slot_id] = values

        try:
            DesktopWorkstationButtonRegistry.from_config(
                slot_config,
                self.agent.os_type,
            )
        except ValidationError as exc:
            first_error = exc.errors(include_url=False)[0]
            field = ".".join(str(part) for part in first_error.get("loc", ()))
            detail = first_error.get("msg", "Invalid button configuration")
            prefix = f"{field}: " if field else ""
            raise ButtonConfigurationError(prefix + detail) from exc

        raw_desktop_kvm = payload.get(
            "desktop_kvm",
            snapshot.get("desktop_kvm", {}),
        )
        try:
            desktop_kvm = DesktopKVMSettings.model_validate(raw_desktop_kvm)
        except ValidationError as exc:
            first_error = exc.errors(include_url=False)[0]
            field = ".".join(str(part) for part in first_error.get("loc", ()))
            detail = first_error.get("msg", "Invalid KVM configuration")
            prefix = f"{field}: " if field else ""
            raise ButtonConfigurationError(prefix + detail) from exc

        existing = _load_existing_config(self.config_path)
        existing["workstation_slots"] = slot_config
        existing["desktop_kvm"] = desktop_kvm.model_dump(mode="json")
        try:
            saved_path = save_config(existing, self.config_path)
        except OSError as exc:
            raise ButtonConfigurationError(
                f"Could not save the desktop configuration: {exc}",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            ) from exc

        self.config_path = saved_path
        self.agent.reconfigure_workstation_buttons(slot_config)
        self.agent.reconfigure_desktop_kvm(desktop_kvm.model_dump(mode="json"))
        return self.configuration()

    def _asset_text(self, filename: str) -> str:
        return (
            resources.files("desk_controller.desktop_agent")
            .joinpath("web", filename)
            .read_text(encoding="utf-8")
        )

    def _handler_class(self):
        owner = self

        class ConfigRequestHandler(BaseHTTPRequestHandler):
            server_version = "DeskAgentConfig/1.0"

            def log_message(self, message: str, *args) -> None:
                logger.debug("%s - %s", self.client_address[0], message % args)

            def _send_headers(
                self,
                status: HTTPStatus,
                content_type: str,
                content_length: int,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(content_length))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; "
                    "script-src 'self'; "
                    "style-src 'self'; "
                    "img-src 'self' data:; "
                    "connect-src 'self'; "
                    "frame-ancestors 'none'; "
                    "base-uri 'none'; "
                    "form-action 'self'",
                )
                self.end_headers()

            def _send_bytes(
                self,
                body: bytes,
                content_type: str,
                status: HTTPStatus = HTTPStatus.OK,
            ) -> None:
                self._send_headers(status, content_type, len(body))
                self.wfile.write(body)

            def _send_json(
                self,
                body: Mapping[str, Any],
                status: HTTPStatus = HTTPStatus.OK,
            ) -> None:
                encoded = json.dumps(body).encode("utf-8")
                self._send_bytes(
                    encoded,
                    "application/json; charset=utf-8",
                    status,
                )

            def _authorized_api_request(self) -> bool:
                return secrets.compare_digest(
                    self.headers.get("X-Desk-Agent-Token", ""),
                    owner.api_token,
                )

            def _require_api_token(self) -> bool:
                if self._authorized_api_request():
                    return True
                self._send_json(
                    {"detail": "Invalid local configuration token."},
                    HTTPStatus.FORBIDDEN,
                )
                return False

            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                try:
                    if path in {"/", "/index.html"}:
                        page = owner._asset_text("index.html").replace(
                            "__DESK_AGENT_TOKEN__",
                            owner.api_token,
                        )
                        self._send_bytes(
                            page.encode("utf-8"),
                            "text/html; charset=utf-8",
                        )
                        return
                    if path == "/assets/styles.css":
                        self._send_bytes(
                            owner._asset_text("styles.css").encode("utf-8"),
                            "text/css; charset=utf-8",
                        )
                        return
                    if path == "/assets/app.js":
                        self._send_bytes(
                            owner._asset_text("app.js").encode("utf-8"),
                            "text/javascript; charset=utf-8",
                        )
                        return
                    if path == "/api/v1/buttons":
                        if not self._require_api_token():
                            return
                        self._send_json(owner.configuration())
                        return
                except (OSError, ButtonConfigurationError) as exc:
                    logger.exception("Could not serve desktop button editor")
                    self._send_json(
                        {"detail": str(exc)},
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                self._send_json(
                    {"detail": "Not found."},
                    HTTPStatus.NOT_FOUND,
                )

            def do_PUT(self) -> None:
                if urlsplit(self.path).path != "/api/v1/buttons":
                    self._send_json(
                        {"detail": "Not found."},
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                if not self._require_api_token():
                    return

                try:
                    content_length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    content_length = 0
                if not 0 < content_length <= MAX_REQUEST_BYTES:
                    self._send_json(
                        {"detail": "Invalid request size."},
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    )
                    return
                try:
                    payload = json.loads(
                        self.rfile.read(content_length).decode("utf-8")
                    )
                    result = owner.update_configuration(payload)
                except json.JSONDecodeError:
                    self._send_json(
                        {"detail": "Request body must be valid JSON."},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                except ButtonConfigurationError as exc:
                    self._send_json({"detail": str(exc)}, exc.status)
                    return
                except Exception:
                    logger.exception("Could not update desktop buttons")
                    self._send_json(
                        {"detail": "Could not update desktop buttons."},
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                self._send_json(result)

        return ConfigRequestHandler

    def start(self) -> str:
        if self._httpd is not None:
            return self.url
        try:
            self._httpd = ThreadingHTTPServer(
                (self.host, self.requested_port),
                self._handler_class(),
            )
        except OSError:
            logger.warning(
                "Port %s is unavailable; selecting another loopback port",
                self.requested_port,
            )
            self._httpd = ThreadingHTTPServer(
                (self.host, 0),
                self._handler_class(),
            )
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="desk-agent-config",
            daemon=True,
        )
        self._thread.start()
        logger.info("Desktop button editor available at %s", self.url)
        return self.url

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._httpd = None
        self._thread = None
