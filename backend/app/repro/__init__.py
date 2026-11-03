"""Modal reproduction package (Step 5).

Executes a frozen ``.buglens.json`` contract inside Modal against the exact
published commit: GitHub source resolution, contract freezing, background
jobs, the sandbox runner, and run persistence. No status here claims a bug
is verified or fixed; verdicts are REPRODUCED / NEEDS_REPRODUCTION /
BLOCKED only.
"""

from .models import RunState, Verdict

__all__ = ["RunState", "Verdict"]
