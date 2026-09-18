import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mnd.protocol import ACTIONS, Gate, GuestStopped, allowed_actions, atomic_json, context_key


class ProtocolTests(unittest.TestCase):
    def test_gate_rejects_stale_release_and_waits_for_its_own_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = []
            worker = threading.Thread(target=lambda: result.append(Gate(root).wait({"frame": 40})))
            worker.start()
            try:
                deadline = time.monotonic() + 2
                while not (root / "state.json").exists():
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.005)
                state = json.loads((root / "state.json").read_text())
                atomic_json(root / "control.json", {"gate": "old-token", "timeline": "wrong"})
                worker.join(timeout=0.05)
                self.assertTrue(worker.is_alive(), "A stale command released the captured process")
                atomic_json(root / "control.json", {"gate": state["gate"], "timeline": "child-1"})
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(result[0]["timeline"], "child-1")
                self.assertFalse((root / "control.json").exists())
            finally:
                (root / "stop.json").touch()
                worker.join(timeout=2)

    def test_waiting_guest_can_be_stopped_for_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "stop.json").touch()
            with self.assertRaises(GuestStopped):
                Gate(root).wait({"frame": 0})

    def test_retry_exclusions_are_state_specific_and_never_exhaust_choices(self):
        state = {
            "level": {"world": 1, "stage": 1, "area": 0},
            "player": {"x": 150, "y": 80, "jump_phase": "grounded"},
        }
        key = context_key(state)
        self.assertNotIn("right", allowed_actions(state, [{"context": key, "action": "right"}]))
        state["player"]["jump_phase"] = "rising"
        self.assertIn("right", allowed_actions(state, [{"context": key, "action": "right"}]))
        state["player"]["jump_phase"] = "grounded"
        avoid = [{"context": key, "action": action} for action in ACTIONS]
        self.assertEqual(allowed_actions(state, avoid), ACTIONS)


if __name__ == "__main__":
    unittest.main()
