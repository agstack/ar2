# SPDX-License-Identifier: EUPL-1.2
# Copyright (c) 2026 AgStack project contributors.
# Licensed under the EUPL, Version 1.2; see the LICENSE file for the full text.

from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def mint_credentials():
    """Mint all credential variants fresh at test time. No dated artifacts,
    no expiry cliff, and the minter is exercised on every run."""
    from app.tests.testkit.mint_test_authority_credentials import mint_authority
    mint_authority(Path(__file__).parent / "testkit" / "dev_keys")
