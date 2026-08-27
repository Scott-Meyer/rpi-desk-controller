import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from pydantic import ValidationError

from desk_controller.core.workstation_slots import (
    WorkstationSlotCommand,
    WorkstationSlotManifest,
    parse_workstation_topic,
)
from desk_controller.desktop_agent.agent import DesktopAgent
from desk_controller.desktop_agent.media import NowPlayingState
from desk_controller.desktop_agent.workstation_buttons import (
    DesktopWorkstationButtonRegistry,
)
from desk_controller.pi_controller.workstation_deck import (
    SelectedWorkstationDeck,
)


class WorkstationSlotContractTests(unittest.TestCase):
    def test_manifest_rejects_duplicate_slot_ids(self):
        with self.assertRaises(ValidationError):
            WorkstationSlotManifest.model_validate(
                {
                    "device_id": "desktop",
                    "slots": [
                        {"slot_id": "primary", "label": "One"},
                        {"slot_id": "primary", "label": "Two"},
                    ],
                }
            )

    def test_exact_workstation_topics_are_parsed(self):
        self.assertEqual(
            parse_workstation_topic("desk/workstation-b/deck/state"),
            ("workstation-b", "state"),
        )
        self.assertIsNone(
            parse_workstation_topic("desk/workstation-b/deck/state/extra")
        )

    def test_commands_reject_remote_execution_details(self):
        with self.assertRaises(ValidationError):
            WorkstationSlotCommand.model_validate(
                {
                    "slot_id": "primary",
                    "action": "activate",
                    "executable": "/tmp/untrusted",
                }
            )


class DesktopWorkstationButtonRegistryTests(unittest.TestCase):
    def make_registry(self, os_type="darwin"):
        return DesktopWorkstationButtonRegistry.from_config(
            {
                "primary": {
                    "label": "CHAT",
                    "icon": "none",
                    "accent_color": [1, 2, 3],
                    "process_names": ["Slack", "Slack.exe"],
                    "launch_target": "Slack",
                }
            },
            os_type,
        )

    @patch(
        "desk_controller.desktop_agent.workstation_buttons.psutil.process_iter",
        return_value=[
            SimpleNamespace(info={"name": "Slack.exe", "exe": "C:/Apps/Slack.exe"})
        ],
    )
    def test_active_state_matches_configured_process_names(self, _processes):
        self.assertEqual(
            self.make_registry("windows").active_state(),
            {"primary": True},
        )

    @patch("desk_controller.desktop_agent.workstation_buttons.subprocess.run")
    def test_macos_activation_uses_open_without_a_shell(self, run):
        run.return_value.returncode = 0

        self.assertTrue(self.make_registry().activate("primary"))

        run.assert_called_once_with(
            ["open", "-a", "Slack"],
            check=False,
            capture_output=True,
            timeout=10,
        )

    def test_audio_mute_button_uses_the_local_audio_driver(self):
        registry = DesktopWorkstationButtonRegistry.from_config(
            {
                "15": {
                    "label": "MUTE",
                    "icon": "mute",
                    "action_type": "audio_mute",
                }
            },
            "darwin",
        )
        audio_driver = Mock()
        audio_driver.toggle_mute.return_value = True

        self.assertEqual(
            registry.active_state(audio_muted=True),
            {"15": True},
        )
        self.assertTrue(registry.activate("15", audio_driver))
        audio_driver.toggle_mute.assert_called_once_with()

    def test_media_button_uses_live_title_icon_and_playback_state(self):
        registry = DesktopWorkstationButtonRegistry.from_config(
            {
                "14": {
                    "label": "PLAY/PAUSE",
                    "icon": "play",
                    "accent_color": [80, 180, 255],
                    "action_type": "media_play_pause",
                }
            },
            "darwin",
        )
        media = NowPlayingState(
            available=True,
            title="Blue Monday",
            artist="New Order",
            is_playing=True,
        )

        presentation = registry.manifest_slots(media=media)[0]

        self.assertEqual(presentation.label, "Blue\nMonday")
        self.assertEqual(presentation.icon, "pause")
        self.assertEqual(
            registry.active_state(media=media),
            {"14": True},
        )

    def test_media_button_uses_local_media_driver(self):
        registry = DesktopWorkstationButtonRegistry.from_config(
            {
                "14": {
                    "label": "PLAY/PAUSE",
                    "action_type": "media_play_pause",
                }
            },
            "darwin",
        )
        media_driver = Mock()
        media_driver.toggle_play_pause.return_value = True

        self.assertTrue(registry.activate("14", media_driver=media_driver))
        media_driver.toggle_play_pause.assert_called_once_with()

    @patch("desk_controller.desktop_agent.workstation_buttons.subprocess.run")
    def test_open_action_uses_the_platform_default_application(self, run):
        run.return_value.returncode = 0
        registry = DesktopWorkstationButtonRegistry.from_config(
            {
                "3": {
                    "label": "DOCS",
                    "action_type": "open",
                    "launch_target": "https://example.com/docs",
                }
            },
            "darwin",
        )

        self.assertTrue(registry.activate("3"))
        run.assert_called_once_with(
            ["open", "https://example.com/docs"],
            check=False,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(registry.active_state(), {"3": False})

    @patch("desk_controller.desktop_agent.workstation_buttons.subprocess.run")
    def test_macos_hotkey_action_uses_validated_key_code(self, run):
        run.return_value.returncode = 0
        registry = DesktopWorkstationButtonRegistry.from_config(
            {
                "4": {
                    "label": "SHORTCUT",
                    "action_type": "hotkey",
                    "shortcut": "command+shift+k",
                }
            },
            "darwin",
        )

        self.assertTrue(registry.activate("4"))
        run.assert_called_once_with(
            [
                "osascript",
                "-e",
                (
                    'tell application "System Events" to key code 40 '
                    "using {shift down, command down}"
                ),
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )


class SelectedWorkstationDeckTests(unittest.TestCase):
    def setUp(self):
        self.deck = SelectedWorkstationDeck(
            {
                0: {
                    "enabled": True,
                    "action_type": "kvm_toggle",
                    "label": "HOST",
                },
                8: {
                    "enabled": True,
                    "action_type": "workstation_slot",
                    "slot_id": "primary",
                    "label": "APP SLOT",
                    "icon": "none",
                },
            }
        )

    def ingest(self, device, kind, payload):
        return self.deck.ingest(
            f"desk/{device}/"
            + ("availability" if kind == "availability" else f"deck/{kind}"),
            payload if isinstance(payload, str) else json.dumps(payload),
        )

    def test_selected_host_gets_profile_and_active_state(self):
        self.ingest("workstation-b", "availability", "online")
        self.ingest(
            "workstation-b",
            "manifest",
            {
                "device_id": "workstation-b",
                "slots": [
                    {
                        "slot_id": "primary",
                        "label": "SLACK",
                        "icon": "none",
                        "accent_color": [5, 6, 7],
                    }
                ],
            },
        )
        self.ingest(
            "workstation-b",
            "state",
            {
                "device_id": "workstation-b",
                "active": {"primary": True},
            },
        )

        resolved = self.deck.resolve("workstation-b")

        self.assertEqual(resolved[8]["label"], "SLACK")
        self.assertTrue(resolved[8]["_agent_online"])
        self.assertTrue(resolved[8]["_slot_active"])
        self.assertEqual(resolved[0]["label"], "HOST")

    def test_unconfigured_physical_keys_are_implicit_workstation_slots(self):
        self.ingest("workstation-b", "availability", "online")
        self.ingest(
            "workstation-b",
            "manifest",
            {
                "device_id": "workstation-b",
                "slots": [{"slot_id": "15", "label": "MUTE"}],
            },
        )

        resolved = self.deck.resolve("workstation-b")

        self.assertEqual(resolved[14]["action_type"], "workstation_slot")
        self.assertEqual(resolved[14]["slot_id"], "15")
        self.assertEqual(resolved[14]["label"], "MUTE")
        self.assertTrue(resolved[14]["_implicit_workstation_slot"])

    def test_other_host_state_does_not_leak_into_selected_host(self):
        self.ingest("desktop", "availability", "online")
        self.ingest(
            "desktop",
            "manifest",
            {
                "device_id": "desktop",
                "slots": [{"slot_id": "primary", "label": "WINDOWS APP"}],
            },
        )

        resolved = self.deck.resolve("workstation-b")

        self.assertEqual(resolved[8]["label"], "")
        self.assertEqual(resolved[8]["icon"], "none")
        self.assertFalse(resolved[8]["_agent_online"])

    def test_online_host_leaves_unadvertised_slots_blank(self):
        self.ingest("workstation-b", "availability", "online")
        self.ingest(
            "workstation-b",
            "manifest",
            {"device_id": "workstation-b", "slots": []},
        )

        resolved = self.deck.resolve("workstation-b")

        self.assertEqual(resolved[8]["label"], "")
        self.assertEqual(resolved[8]["icon"], "none")
        self.assertFalse(resolved[8]["_slot_configured"])

    def test_invalid_or_mismatched_messages_are_rejected(self):
        self.assertIsNone(
            self.ingest(
                "workstation-b",
                "manifest",
                {"device_id": "desktop", "slots": []},
            )
        )
        self.assertIsNone(self.ingest("workstation-b", "state", "not-json"))


class DesktopAgentSlotTests(unittest.TestCase):
    def test_valid_slot_command_is_activated_locally(self):
        agent = object.__new__(DesktopAgent)
        agent.device_id = "workstation-b"
        agent.audio_driver = None
        agent.workstation_buttons = Mock()
        agent.mqtt = Mock()
        agent.mqtt.publish.return_value = True
        agent._last_slot_state = None
        agent.workstation_buttons.active_state.return_value = {"primary": True}
        agent.workstation_buttons.manifest_slots.return_value = []

        agent._on_mqtt_message(
            "desk/workstation-b/deck/command",
            WorkstationSlotCommand(slot_id="primary").model_dump_json(),
        )

        agent.workstation_buttons.activate.assert_called_once_with(
            "primary",
            None,
            None,
        )
        published_topics = [call.args[0] for call in agent.mqtt.publish.call_args_list]
        self.assertIn("desk/workstation-b/deck/state", published_topics)

    def test_retried_slot_command_is_acknowledged_without_executing_twice(self):
        agent = object.__new__(DesktopAgent)
        agent.device_id = "workstation-b"
        agent.audio_driver = None
        agent.media_driver = None
        agent.workstation_buttons = Mock()
        agent.workstation_buttons.activate.return_value = True
        agent.workstation_buttons.active_state.return_value = {"primary": True}
        agent.workstation_buttons.manifest_slots.return_value = []
        agent.mqtt = Mock()
        agent.mqtt.publish.return_value = True
        agent._last_slot_manifest = None
        agent._last_slot_state = None
        agent._slot_command_lock = threading.RLock()
        agent._slot_result_cache = {}
        command = WorkstationSlotCommand(
            request_id=uuid4(),
            slot_id="primary",
        ).model_dump_json()

        agent._on_mqtt_message("desk/workstation-b/deck/command", command)
        agent._on_mqtt_message("desk/workstation-b/deck/command", command)

        agent.workstation_buttons.activate.assert_called_once()
        acknowledgements = [
            mqtt_call
            for mqtt_call in agent.mqtt.publish.call_args_list
            if mqtt_call.args[0] == "desk/workstation-b/deck/command_result"
        ]
        self.assertEqual(len(acknowledgements), 2)

    def test_live_media_title_republishes_the_manifest(self):
        agent = object.__new__(DesktopAgent)
        agent.device_id = "workstation-b"
        agent.audio_driver = None
        agent._last_slot_manifest = None
        agent._last_slot_state = None
        agent.mqtt = Mock()
        agent.mqtt.publish.return_value = True
        agent.workstation_buttons = DesktopWorkstationButtonRegistry.from_config(
            {
                "14": {
                    "label": "PLAY/PAUSE",
                    "action_type": "media_play_pause",
                }
            },
            "darwin",
        )
        agent.media_driver = Mock()
        agent.media_driver.get_now_playing.side_effect = [
            NowPlayingState(
                available=True,
                title="First Song",
                is_playing=True,
            ),
            NowPlayingState(
                available=True,
                title="Next Song",
                is_playing=True,
            ),
        ]

        agent._publish_workstation_slots()
        agent._publish_workstation_slots()

        manifests = [
            call.args[1]
            for call in agent.mqtt.publish.call_args_list
            if call.args[0] == "desk/workstation-b/deck/manifest"
        ]
        self.assertEqual(len(manifests), 2)
        self.assertEqual(manifests[0]["slots"][0]["label"], "First Song")
        self.assertEqual(manifests[1]["slots"][0]["label"], "Next Song")
