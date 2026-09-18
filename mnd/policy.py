"""Typed Jev decisions for the complete controller vocabulary."""

import time

from typesafe_mario.policy import Decision, TypeSafePolicy

from .actions import Action, descriptions


class ControllerPolicy(TypeSafePolicy):
    def choose(self, snapshot, actions):
        # The upstream parser remains useful, but its seven-value Action enum
        # cannot decode left_jump/down responses. Decode our own enum explicitly.
        descriptions_by_action = descriptions()
        questions = {
            "next_action": self._Choice(
                criteria={a.value: descriptions_by_action[a] for a in actions},
                instructions={
                    "question": "Which controller macro should Mario commit to next?",
                    "goal": snapshot.goal,
                    "timing": "The selected action is held for 8 emulator frames.",
                    "geometry": (
                        "Use terrain.observation_reliability and local_map. While airborne, "
                        "prefer reliable landing surfaces and the last grounded preview. "
                        "Jump over solid obstacles when reachable; visible pipe mouths "
                        "can be entrances, so do not automatically jump over them."
                    ),
                    "enemy_timing": (
                        "Use hazard projections and enemy spacing. Start a jump when "
                        "jump_must_start_this_decision or contact_within_reaction_horizon "
                        "warns of imminent contact; waiting can miss the takeoff window."
                    ),
                    "stall": (
                        "Judge a move by its observed displacement. If left is blocked by "
                        "a pipe or block, consider left_jump or left_run_jump. Continuing "
                        "to press into a solid wall does not escape it."
                    ),
                },
            ),
            "jump_needed": self._Noul(
                instructions=(
                    "Do the visible terrain, enemies, and jump phase require beginning "
                    "or sustaining a jump in the chosen direction now? A pipe entrance "
                    "may instead require walking into its mouth or pressing down."
                )
            ),
            "danger": self._Score(
                instructions="How dangerous is Mario's immediate situation?",
                criteria=[
                    "Safe open movement",
                    "Potential obstacle or enemy soon",
                    "Immediate collision, fall, or enemy threat",
                ],
            ),
        }
        started = time.perf_counter()
        response = self._client.system_one(state=snapshot.to_state(), questions=questions)
        choice = self._answer(response, "next_action", "choices")
        action = Action(str(choice.choice))
        if action not in actions:
            raise ValueError("Jev selected an action that was not offered")
        return Decision(
            action=action,
            confidence=float(choice.confidence),
            probabilities={str(k): float(v) for k, v in dict(choice.probabilities).items()},
            latency_ms=(time.perf_counter() - started) * 1000,
            jump_needed_probability=float(self._answer(response, "jump_needed", "nouls").noul),
            danger_score=float(self._answer(response, "danger", "scores").score),
        )
