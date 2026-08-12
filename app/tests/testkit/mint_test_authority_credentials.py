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
        key = private_pem if name != "untrusted_authority" else generate_keypair_pem()
        token = jwt.encode(claims, key, algorithm="EdDSA", headers={"typ": "vc+sd-jwt", "kid": "pancake-test-1"})
        (out_dir / f"{name}.sdjwt").write_text(f"{token}~")

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
