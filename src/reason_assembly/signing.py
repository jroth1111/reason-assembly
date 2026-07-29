"""Optional Ed25519 signatures for sealed run manifests."""

from __future__ import annotations

from pathlib import Path

from .artifacts import RunStore


def _serialization():
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:
        raise RuntimeError(
            "manifest signing requires the 'signing' extra: pip install reason-assembly[signing]"
        ) from exc
    return serialization


def sign_manifest(store: RunStore, private_key_path: Path) -> str:
    serialization = _serialization()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("signing key must be an Ed25519 private key")
    manifest = store.read_json("manifest.json")
    digest = manifest.get("integrity_sha256") or store.seal_manifest()
    signature = key.sign(digest.encode()).hex()
    manifest = store.read_json("manifest.json")
    manifest["signature"] = signature
    store.write_json("manifest.json", manifest)
    return signature


def verify_signature(store: RunStore, public_key_path: Path) -> bool:
    serialization = _serialization()
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    key = serialization.load_pem_public_key(public_key_path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise RuntimeError("verification key must be an Ed25519 public key")
    manifest = store.read_json("manifest.json")
    digest = manifest.get("integrity_sha256")
    signature = manifest.get("signature")
    if not digest or not signature:
        return False
    try:
        key.verify(bytes.fromhex(signature), digest.encode())
    except (InvalidSignature, ValueError):
        return False
    return True
