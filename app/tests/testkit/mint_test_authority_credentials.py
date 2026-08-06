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
pancake_path = "/home/rajat/Downloads/rnaura_work/pancake/services"
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
    
    AUTHORITY_KEY_PATH = Path(__file__).parent / "dev_keys/authority_issuer_private.pem"
    private_pem = AUTHORITY_KEY_PATH.read_bytes()

    now = int(time.time())

    creds = {
        "valid_authority": base_claims(DEFAULT_ISSUER_ID, now + 30*24*3600, idx=1, scope="demo-recall"),
        "expired_authority": base_claims(DEFAULT_ISSUER_ID, now - 3600, idx=2, scope="demo-recall"),
        "revoked_authority": base_claims(DEFAULT_ISSUER_ID, now + 30*24*3600, idx=3, scope="demo-recall"),
        "outofscope_authority": base_claims(DEFAULT_ISSUER_ID, now + 30*24*3600, idx=4, scope="other-jurisdiction"),
    }
    
    for name, claims in creds.items():
        (out_dir / f"{name}.sdjwt").write_text(sdjwt.issue(claims, [], private_pem, DEFAULT_KID))

    # revoked_authority: set bit 3 in the test status list
    write_status_list(out_dir, revoked_indices=[3])
    print(f"To use them, point AR_TRUSTED_ISSUER_PUBKEY to {out_dir}/authority_issuer_public.pem")

import zlib

def write_status_list(out_dir: Path, revoked_indices: list[int]):
    import base64
    # Create a 16-byte bitstring (128 bits)
    bitstring = bytearray(16)
    for idx in revoked_indices:
        byte_idx = idx // 8
        bit_idx = idx % 8
        bitstring[byte_idx] |= (1 << bit_idx)
    
    compressed = zlib.compress(bitstring)
    encoded = base64.urlsafe_b64encode(compressed).rstrip(b"=").decode("ascii")
    (out_dir / "status_list.txt").write_text(encoded)

if __name__ == "__main__":
    mint_authority(Path(__file__).parent / "dev_keys")
