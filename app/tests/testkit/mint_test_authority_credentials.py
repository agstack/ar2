"""Mint test Authority credentials (vct: agstack.org/credentials/traceforward-authority/v1).

Usage:
    python -m testkit.mint_test_authority_credentials

Generates:
    valid_authority.sdjwt
    expired_authority.sdjwt
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from ulid import ULID
import sys
import os

# Add pancake to path to use its sdjwt libraries for testing
pancake_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../pancake/services"))
sys.path.append(pancake_path)

from pancake_services.grants import sdjwt
from pancake_services.grants.issuer import DEFAULT_ISSUER_ID, DEFAULT_KID, generate_keypair_pem

STATUS_URI = "http://localhost:8100/grants/status-list"
AUTHORITY_VCT = "agstack.org/credentials/traceforward-authority/v1"

def base_claims(issuer_id: str, exp: int, idx: int, scope: str = "demo-recall") -> dict:
    jti = str(ULID())
    return {
        "iss": issuer_id,
        "sub": "authority@demo.agstack.org",
        "iat": int(time.time()),
        "exp": exp,
        "jti": jti,
        "vct": AUTHORITY_VCT,
        "scope": scope,
        "status": {"status_list": {"uri": STATUS_URI, "idx": idx}},
    }

def mint_authority(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    
    pancake_dev_key = Path(__file__).parent.parent.parent / "pancake/services/pancake_services/grants/testkit/dev_keys/dev_issuer_private.pem"
    private_pem = pancake_dev_key.read_bytes()

    now = int(time.time())
    future = now + 30 * 24 * 3600
    past = now - 3600

    creds = {}

    # We use the raw sdjwt issue function from Pancake, but with our VCT
    # Authority credentials don't strictly require field disclosures, but we pass an empty list
    creds["valid_authority"] = sdjwt.issue(
        base_claims(DEFAULT_ISSUER_ID, future, idx=1),
        [], private_pem, DEFAULT_KID,
    )
    
    for name, cred in creds.items():
        (out_dir / f"{name}.sdjwt").write_text(cred)

    print(f"Minted Authority credentials into {out_dir}")
    print(f"To use them, point AR_TRUSTED_ISSUER_PUBKEY to {out_dir}/authority_issuer_public.pem")

if __name__ == "__main__":
    mint_authority(Path(__file__).parent / "dev_keys")
