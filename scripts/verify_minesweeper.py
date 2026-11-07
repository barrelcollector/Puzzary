"""Generation, privacy, rule, and progress checks for Minesweeper."""
import json
from copy import deepcopy
from types import SimpleNamespace
import unittest

from app.puzzle_types.minesweeper import (
    check_answer, collaboration, count_progress, counts_for, generate,
    neighbors, normalize_progress,
)


class MinesweeperTests(unittest.TestCase):
    def setUp(self):
        self.solution = {
            "kind": "minesweeper", "width": 3, "height": 3, "mine_count": 1,
            "mines": ["1,1"], "counts": counts_for(3, 3, {"1,1"}),
        }

    def action(self, state, kind, target=""):
        return collaboration(state, {"kind": kind, "target": target, "changes": {}}, self.solution)

    def test_seeded_generation_and_private_payloads(self):
        options = {"width": 12, "height": 8, "mine_count": 17}
        first = generate("", "easy", seed="repeat", generation_options=options)
        second = generate("", "hard", seed="repeat", generation_options=options)
        self.assertEqual(first.solution_payload, second.solution_payload)
        self.assertEqual(len(first.solution_payload["mines"]), 17)
        self.assertEqual(len(set(first.solution_payload["mines"])), 17)
        self.assertNotIn("mines", first.play_payload)
        self.assertNotIn("counts", first.play_payload)
        self.assertNotIn("mines", first.print_payload)
        self.assertNotEqual(first.solution_payload["mines"], generate("", "easy", seed="different", generation_options=options).solution_payload["mines"])
        mines = set(first.solution_payload["mines"])
        for cell, value in first.solution_payload["counts"].items():
            self.assertEqual(int(value), sum(item in mines for item in neighbors(cell, 12, 8)))

    def test_generation_validation(self):
        for options in (
            {"width": 4}, {"width": 31}, {"height": 4}, {"height": 25},
            {"mine_count": 0}, {"width": 5, "height": 5, "mine_count": 25},
            {"width": 5.5}, {"height": True}, {"mine_count": "many"},
            {"open_connected_safe_spaces": "sometimes"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                generate("", "easy", generation_options=options)

    def test_connected_safe_space_setting(self):
        solution = {
            "kind": "minesweeper", "width": 3, "height": 3, "mine_count": 1,
            "mines": ["2,2"], "counts": counts_for(3, 3, {"2,2"}),
        }
        action = {"kind": "reveal_cell", "target": "0,0", "changes": {}}
        default_state = collaboration({}, action, solution)
        self.assertEqual(default_state["status"], "won")
        self.assertEqual(len(default_state["answers"]), 8)

        single_space = deepcopy(solution)
        single_space["settings"] = {"open_connected_safe_spaces": False}
        single_state = collaboration({}, action, single_space)
        self.assertEqual(single_state["status"], "playing")
        self.assertEqual(single_state["answers"], {"0,0": "0"})

        generated = generate("", "easy", seed="connected-setting", generation_options={
            "width": 5, "height": 5, "mine_count": 3, "open_connected_safe_spaces": "no",
        })
        self.assertFalse(generated.play_payload["settings"]["open_connected_safe_spaces"])
        self.assertFalse(generated.solution_payload["settings"]["open_connected_safe_spaces"])

    def test_flag_reveal_chord_and_reset(self):
        state = self.action({}, "reveal_cell", "0,0")
        self.assertEqual(state["answers"], {"0,0": "1"})
        state = self.action(state, "flag", "1,1")
        self.assertEqual(state["answers"]["1,1"], "F")
        state = self.action(state, "reveal_cell", "0,0")
        self.assertEqual(state["answers"]["1,0"], "1")
        self.assertEqual(state["answers"]["0,1"], "1")
        state = self.action(state, "flag", "1,1")
        self.assertNotIn("1,1", state["answers"])
        self.assertEqual(self.action(state, "reset"), {"answers": {}, "status": "playing", "exploded": ""})

    def test_loss_win_hint_and_solution_reveal(self):
        lost = self.action({}, "reveal_cell", "1,1")
        self.assertEqual(lost["status"], "lost")
        self.assertEqual(lost["exploded"], "1,1")
        self.assertEqual(lost["answers"]["1,1"], "M")
        self.assertEqual(count_progress(lost, self.solution), (0, 8))
        hinted = self.action({}, "hint")
        self.assertTrue(any(value.isdigit() for value in hinted["answers"].values()))
        revealed = self.action({}, "reveal")
        self.assertEqual(revealed["status"], "revealed")
        self.assertEqual(count_progress(revealed, self.solution), (7, 8))
        state = {}
        for cell in self.solution["counts"]:
            state = self.action(state, "reveal_cell", cell)
        self.assertEqual(state["status"], "won")
        self.assertEqual(count_progress(state, self.solution), (8, 8))
        self.assertTrue(check_answer(json.dumps(state), self.solution)[0])

    def test_untrusted_progress_and_invalid_actions(self):
        forged = SimpleNamespace(answers={cell: value for cell, value in self.solution["counts"].items()})
        self.assertEqual(normalize_progress(forged, self.solution), {"answers": {}, "status": "playing", "exploded": ""})
        with self.assertRaises(ValueError): self.action({}, "reveal_cell", "9,9")
        with self.assertRaises(ValueError): self.action({}, "reveal_cell", "bad")
        with self.assertRaises(ValueError): collaboration({}, {"kind": "patch", "changes": {"0,0": "0"}}, self.solution)
        with self.assertRaises(ValueError): self.action({}, "dance")


if __name__ == "__main__":
    unittest.main(verbosity=2)
