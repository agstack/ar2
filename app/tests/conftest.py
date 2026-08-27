import base64
import json
import time
from pathlib import Path

import pytest

# Grant fixtures are minted by Pancake's testkit and committed here, so they
# carry a real expiry. Every one of these must be in-window for its test to
# mean anything: an expired "valid" fails outright, while an expired
# "tampered" or "revoked" is still rejected -- but for expiry rather than
# for the property under test, and the test passes while testing nothing.
# (That happened on 2026-08-20; four tests failed on every branch and the
# tamper/revocation checks had been vacuous for six days before anyone saw.)
GRANT_FIXTURES_NEEDING_VALIDITY = (
    "valid.sdjwt",
    "revoked.sdjwt",
    "tampered.sdjwt",
    "wrong_geoid.sdjwt",
)

REMINT_COMMAND = (
    "PYTHONPATH=<pancake-checkout>/services python -m "
    "pancake_services.grants.testkit.mint_test_credentials "
    "--out app/tests/testkit/dev_keys --days 365"
)


def _grant_fixture_expiry_guard(dev_keys: Path, min_remaining_days: float = 30) -> None:
    stale = []
    for name in GRANT_FIXTURES_NEEDING_VALIDITY:
        token = (dev_keys / name).read_text().strip().split("~")[0]
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)
        remaining = (exp - time.time()) / 86400
        if remaining < min_remaining_days:
            stale.append(f"  {name}: {remaining:.0f} day(s) of validity remaining")
    if stale:
        raise RuntimeError(
            "Grant fixtures are expired or expiring; their tests would pass "
            "vacuously or fail spuriously. Re-mint them (from a pancake "
            f"checkout, run in this repo's root):\n{REMINT_COMMAND}\n"
            + "\n".join(stale)
        )


@pytest.fixture(scope="session", autouse=True)
def mint_credentials():
    """Mint the authority credential variants fresh at test time, and refuse
    to run against grant fixtures that are close to their expiry cliff."""
    dev_keys = Path(__file__).parent / "testkit" / "dev_keys"
    from app.tests.testkit.mint_test_authority_credentials import mint_authority
    mint_authority(dev_keys)
    _grant_fixture_expiry_guard(dev_keys)
