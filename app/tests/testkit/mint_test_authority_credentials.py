# SPDX-License-Identifier: EUPL-1.2
# Copyright (c) 2026 AgStack project contributors.
# Licensed under the EUPL, Version 1.2; see the LICENSE file for the full text.

"""Mint test Authority credentials (vct: agstack.org/credentials/traceforward-authority/v1).

Usage:
    python -m app.tests.testkit.mint_test_authority_credentials

Generates:
    valid_authority.sdjwt
    expired_authority.sdjwt
"""
from __future__ import annotations

import time
from pathlib import Path

import jwt
from ulid import ULID

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

def generate_keypair_pem():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_pem

def _still_valid(path: Path, min_remaining_s: int = 24 * 3600) -> bool:
    """True if this credential exists and will not expire during the run.

    Read without verifying: the point is the expiry, and the file is a test
    fixture this module minted itself. Anything unreadable is treated as absent
    and re-minted, so a corrupt fixture repairs itself rather than failing the
    suite with a confusing error somewhere else.
    """
    if not path.exists():
        return False
    try:
        token = path.read_text().rstrip("~")
        claims = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
        return claims.get("exp", 0) > time.time() + min_remaining_s
    except Exception:  # noqa: BLE001
        return False


def mint_authority(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    
    AUTHORITY_KEY_PATH = out_dir / "authority_issuer_private.pem"
    private_pem = AUTHORITY_KEY_PATH.read_bytes()

    now = int(time.time())

    creds = {
        "valid_authority": base_claims("did:web:pancake.test", now + 30*24*3600, idx=1, scope="demo-recall"),
        "expired_authority": base_claims("did:web:pancake.test", now - 3600, idx=2, scope="demo-recall"),
        "revoked_authority": base_claims("did:web:pancake.test", now + 30*24*3600, idx=3, scope="demo-recall"),
        "outofscope_authority": base_claims("did:web:pancake.test", now + 30*24*3600, idx=4, scope="other-jurisdiction"),
        "global_authority": base_claims("did:web:pancake.test", now + 30*24*3600, idx=5, scope="global"),
        "untrusted_authority": base_claims("untrusted-issuer", now + 30*24*3600, idx=6, scope="demo-recall"),
    }
    
    for name, claims in creds.items():
        target = out_dir / f"{name}.sdjwt"

        # Only mint what is missing or no longer usable. These files are tracked
        # -- scripts/e2e_traceforward.sh reads them without running pytest first
        # -- and conftest calls this on every test run, so rewriting them
        # unconditionally left the working tree modified after any run. That made
        # the harness report every result as coming from a dirty tree, which is
        # the signal that a result cannot be reproduced from a revision. A
        # warning that fires every single time is one people learn to skip past,
        # so it has to fire only when something is actually uncommitted.
        if name != "expired_authority" and _still_valid(target):
            continue

        key = private_pem if name != "untrusted_authority" else generate_keypair_pem()
        token = jwt.encode(claims, key, algorithm="EdDSA", headers={"typ": "vc+sd-jwt", "kid": "pancake-test-1"})
        target.write_text(f"{token}~")

    # revoked_authority: set bit 3 in the test status list (owner tests use 7)
    write_status_list(out_dir, revoked_indices=[3, 7])
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
