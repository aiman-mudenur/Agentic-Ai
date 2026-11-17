"""OpenClaw Gateway device identity (Ed25519).

The OpenClaw Gateway requires every client to authenticate with a device
identity bound to an approved pairing record. Public/private keys are generated
per installation and persisted as a small JSON file (default:
``~/.course_organizer/openclaw_device.json``, overridable with
``OPENCLAW_DEVICE_IDENTITY_PATH``).

Signature payload (must be byte-identical, UTF-8, pipe separated)::

    v3|<deviceId>|<clientId>|<clientMode>|<role>|<scopesCsv>|<signedAtMs>|<token>|<nonce>|<platformLower>|<deviceFamilyLower>

- ``deviceId`` = lowercase hex SHA-256 of the raw 32-byte public key.
- ``publicKey`` = base64url (no padding) of the raw 32-byte public key.
- ``signature`` = base64url (no padding) Ed25519 signature over the payload.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
)


@dataclass(frozen=True)
class DeviceIdentity:
    device_id: str
    public_key_raw: bytes
    public_key_b64url: str
    private_key: Ed25519PrivateKey

    @property
    def public_key_pem(self) -> str:
        return (
            self.private_key.public_key()
            .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
            .decode("ascii")
        )


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def default_identity_path() -> Path:
    env = (os.getenv("OPENCLAW_DEVICE_IDENTITY_PATH") or "").strip()
    if env:
        return Path(env)
    return Path.home() / ".course_organizer" / "openclaw_device.json"


def _normalize(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


def build_device_auth_payload_v3(
    *,
    device_id: str,
    client_id: str,
    client_mode: str,
    role: str,
    scopes: tuple[str, ...] | list[str],
    signed_at_ms: int,
    token: Optional[str],
    nonce: str,
    platform: Optional[str] = None,
    device_family: Optional[str] = None,
) -> str:
    parts = [
        "v3",
        device_id,
        client_id,
        _normalize(client_mode),
        role,
        ",".join(scopes or []),
        str(int(signed_at_ms)),
        token or "",
        nonce or "",
        _normalize(platform),
        _normalize(device_family),
    ]
    return "|".join(parts)


def sign_payload(private_key: Ed25519PrivateKey, payload: str) -> str:
    return b64url_encode(private_key.sign(payload.encode("utf-8")))


def load_or_create_identity(path: Optional[Path | str] = None) -> DeviceIdentity:
    """Load the persisted device identity, generating and saving one if absent."""
    p = Path(path) if path else default_identity_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            private_key = load_pem_private_key(
                data["privateKeyPem"].encode("ascii"), password=None
            )
            public_key = load_pem_public_key(data["publicKeyPem"].encode("ascii"))
            return _from_keys(private_key, public_key)
        except Exception as exc:  # keep the bad file for inspection, but do not crash auth
            raise RuntimeError(
                f"Could not read OpenClaw device identity at {p}: {exc}. "
                "Delete the file to regenerate a fresh identity."
            ) from exc

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "privateKeyPem": private_key.private_bytes(
                    Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
                ).decode("ascii"),
                "publicKeyPem": public_key.public_bytes(
                    Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
                ).decode("ascii"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return _from_keys(private_key, public_key)


def _from_keys(private_key, public_key) -> DeviceIdentity:
    raw_pub = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return DeviceIdentity(
        device_id=hashlib.sha256(raw_pub).hexdigest(),
        public_key_raw=raw_pub,
        public_key_b64url=b64url_encode(raw_pub),
        private_key=private_key,
    )
