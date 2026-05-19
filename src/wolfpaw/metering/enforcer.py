"""Cap-enforcement stub.

In v1 the default tier is `dev` with effectively-unlimited allowance, so the
check is a no-op. Step 24 (billing) replaces this with a real
period-summary lookup against `usage_summaries` and raises `OverCap` when
the user is at allowance with overage off.
"""

from __future__ import annotations

from uuid import UUID


class OverCap(Exception):
    """Raised when the user has hit their allowance with overage disabled."""


class Enforcer:
    async def check_can_spend(self, user_id: UUID) -> None:
        # v1 no-op. Real check lands in step 24.
        return None


_enforcer = Enforcer()


def get_enforcer() -> Enforcer:
    return _enforcer
