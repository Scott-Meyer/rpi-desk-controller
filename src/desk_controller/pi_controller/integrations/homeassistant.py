"""
Home Assistant REST API Client for office lighting and sensor telemetry.
"""

import logging
import re
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)


class HomeAssistantClient:
    """Interacts with Home Assistant via REST API."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def connection_status(self) -> Dict[str, Any]:
        """Check reachability and token authentication without changing HA."""
        try:
            response = requests.get(
                f"{self.base_url}/api/",
                headers=self.headers,
                timeout=3,
            )
        except requests.RequestException as exc:
            logger.warning("Home Assistant connection check failed: %s", exc)
            return {
                "status": "unreachable",
                "authenticated": False,
                "http_status": None,
                "detail": "Home Assistant could not be reached.",
            }

        if response.status_code == 200:
            return {
                "status": "authenticated",
                "authenticated": True,
                "http_status": 200,
                "detail": "API reachable and access token accepted.",
            }
        if response.status_code in {401, 403}:
            return {
                "status": "authentication_failed",
                "authenticated": False,
                "http_status": response.status_code,
                "detail": "Home Assistant rejected the access token.",
            }
        return {
            "status": "error",
            "authenticated": False,
            "http_status": response.status_code,
            "detail": (f"Home Assistant returned HTTP {response.status_code}."),
        }

    def activate_scene(self, entity_id: str) -> bool:
        """Activate a scene in Home Assistant."""
        return self.call_service("scene.turn_on", entity_id)

    def trigger_automation(self, entity_id: str) -> bool:
        """Trigger an automation in Home Assistant."""
        return self.call_service("automation.trigger", entity_id)

    def call_service(
        self,
        service: str,
        entity_id: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Call any Home Assistant ``domain.service`` with optional data."""
        try:
            domain, service_name = service.strip().split(".", 1)
        except ValueError:
            logger.error("Invalid Home Assistant service: %s", service)
            return False
        if not all(
            re.fullmatch(r"[a-z0-9_]+", value) for value in (domain, service_name)
        ):
            logger.error("Invalid Home Assistant service: %s", service)
            return False

        url = f"{self.base_url}/api/services/{domain}/{service_name}"
        try:
            payload = dict(data or {})
        except (TypeError, ValueError):
            logger.error("Home Assistant service data must be an object")
            return False
        if entity_id.strip():
            payload.setdefault("entity_id", entity_id.strip())
        try:
            res = requests.post(url, json=payload, headers=self.headers, timeout=3)
            if res.status_code == 200:
                logger.info(
                    "Successfully called Home Assistant service %s",
                    service,
                )
                return True
            logger.error(
                "Home Assistant service %s failed (%s): %s",
                service,
                res.status_code,
                res.text,
            )
            return False
        except Exception as e:
            logger.error(f"Failed to communicate with Home Assistant: {e}")
            return False

    def toggle_entity(self, entity_id: str) -> bool:
        """Toggle a switch, light, or script in Home Assistant."""
        domain = entity_id.split(".", 1)[0]
        return self.call_service(f"{domain}.toggle", entity_id)

    def get_state(self, entity_id: str) -> dict:
        """Fetch state of a specific Home Assistant entity."""
        url = f"{self.base_url}/api/states/{entity_id}"
        try:
            res = requests.get(url, headers=self.headers, timeout=3)
            if res.status_code == 200:
                return res.json()
        except Exception as e:
            logger.error(f"Error fetching HA entity state: {e}")
        return {}
