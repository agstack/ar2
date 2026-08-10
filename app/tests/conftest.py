import pytest
from pathlib import Path

@pytest.fixture(scope="session", autouse=True)
def mint_credentials():
    """Mint all credential variants fresh at test time. No dated artifacts,
    no expiry cliff, and the minter is exercised on every run."""
    from app.tests.testkit.mint_test_authority_credentials import mint_authority
    mint_authority(Path(__file__).parent / "testkit" / "dev_keys")
