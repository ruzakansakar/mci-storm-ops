"""Every function a rule can name in `checked_by`: the safety checks plus the helpers that use the rules."""
from backend.rules.autonomy import autonomy_for
from backend.rules.cancellation import cancellation_guard
from backend.rules.compensation import compensation_for
from backend.rules.safety import CHECKS
from backend.rules.tarmac import tarmac_timeline
from backend.rules.check import check
from backend.rules.override import human_override

CHECKERS = {**CHECKS, "compensation_for": compensation_for, "autonomy_for": autonomy_for, "tarmac_timeline": tarmac_timeline,
            "check": check, "human_override": human_override, "cancellation_guard": cancellation_guard}
