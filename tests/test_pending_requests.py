import unittest

from desk_controller.core.pending_requests import PendingWorkstationRequests


class PendingWorkstationRequestsTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.pending = PendingWorkstationRequests(
            timeout=30,
            retry_interval=5,
            monotonic=lambda: self.now,
        )

    def test_request_is_due_on_add_then_retries_on_interval(self):
        request = self.pending.add(2, "laptop", "audio_output", "Desk DAC")

        self.assertEqual(self.pending.due(), [request])
        self.now += 4
        self.assertEqual(self.pending.due(), [])
        self.now += 1
        self.assertEqual(self.pending.due(), [request])
        self.assertEqual(request.attempts, 2)

    def test_request_disappears_when_retry_window_expires(self):
        self.pending.add(2, "laptop", "audio_output", "Desk DAC")

        self.now += 30

        self.assertTrue(self.pending.expire())
        self.assertFalse(self.pending.is_pending("laptop", 2))

    def test_confirmed_audio_state_removes_matching_request(self):
        self.pending.add(2, "laptop", "audio_output", "Desk DAC")

        removed = self.pending.acknowledge_audio(
            "laptop",
            "desk dac",
            lambda left, right: left.casefold() == right.casefold(),
        )

        self.assertTrue(removed)
        self.assertEqual(self.pending.snapshot(), [])

    def test_new_audio_choice_replaces_old_choice_for_the_same_laptop(self):
        self.pending.add(2, "laptop", "audio_output", "Speakers")
        replacement = self.pending.add(
            3,
            "laptop",
            "audio_output",
            "Headphones",
            replace_action_type=True,
        )

        self.assertEqual(self.pending.snapshot(), [replacement])

    def test_acknowledgement_must_come_from_the_target_laptop(self):
        request = self.pending.add(8, "laptop", "workstation_slot", "mute")

        self.assertFalse(
            self.pending.acknowledge(request.request_id, device_id="other")
        )
        self.assertTrue(self.pending.is_pending("laptop", 8))
        self.assertTrue(
            self.pending.acknowledge(request.request_id, device_id="laptop")
        )


if __name__ == "__main__":
    unittest.main()
