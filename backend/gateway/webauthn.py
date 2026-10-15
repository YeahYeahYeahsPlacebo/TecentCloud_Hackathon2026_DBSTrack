"""
WebAuthn ES256 verification (CONTRACT.md §8 open question 1, resolved:
ECDSA P-256 with SHA-256, COSE algorithm ``-7``, user verification required).

The signature the browser hands us is the raw concatenation ``r ‖ s`` (two
32-byte integers, 64 bytes total). ``cryptography`` wants a DER-encoded
signature, so :func:`raw_signature_to_der` converts before verifying.

Nothing in here raises on a bad signature: verification failure is a normal
outcome, not an error, and must surface as ``signature_invalid`` rather than
as a 500. Everything is caught and turned into ``False``.
"""

from __future__ import annotations

import base64
from typing import Any, Mapping, Union

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

# COSE_Key labels for an EC2 key (RFC 8152 / RFC 9053).
COSE_KTY = 1
COSE_ALG = 3
COSE_CRV = -1
COSE_X = -2
COSE_Y = -3

COSE_KTY_EC2 = 2
COSE_ALG_ES256 = -7
COSE_CRV_P256 = 1

RAW_SIGNATURE_LEN = 64  # r ‖ s, 32 bytes each
COORD_LEN = 32


def raw_signature_to_der(raw: bytes) -> bytes:
    """Convert the WebAuthn ``r ‖ s`` signature into DER for ``cryptography``."""
    if len(raw) != RAW_SIGNATURE_LEN:
        raise ValueError(f"ES256 signature must be {RAW_SIGNATURE_LEN} bytes, got {len(raw)}")
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:], "big")
    return encode_dss_signature(r, s)


def der_to_raw_signature(der: bytes) -> bytes:
    """Inverse of :func:`raw_signature_to_der`, for building test vectors."""
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    r, s = decode_dss_signature(der)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def cose_key_to_public_key(cose: Mapping[int, Any]) -> ec.EllipticCurvePublicKey:
    """Build a P-256 public key from a decoded COSE_Key map.

    Only ES256 is accepted: an attacker must not be able to talk the server
    into a weaker curve or algorithm by registering one.
    """
    if cose.get(COSE_KTY) != COSE_KTY_EC2:
        raise ValueError("credential is not an EC2 key")
    if cose.get(COSE_ALG) != COSE_ALG_ES256:
        raise ValueError("credential is not ES256")
    if cose.get(COSE_CRV) != COSE_CRV_P256:
        raise ValueError("credential is not on P-256")
    x = cose.get(COSE_X)
    y = cose.get(COSE_Y)
    if not isinstance(x, (bytes, bytearray)) or not isinstance(y, (bytes, bytearray)):
        raise ValueError("COSE_Key is missing x or y")
    if len(x) != COORD_LEN or len(y) != COORD_LEN:
        raise ValueError("P-256 coordinates must be 32 bytes each")
    numbers = ec.EllipticCurvePublicNumbers(
        x=int.from_bytes(x, "big"), y=int.from_bytes(y, "big"), curve=ec.SECP256R1()
    )
    return numbers.public_key()


def public_key_to_pem(public_key: ec.EllipticCurvePublicKey) -> str:
    return public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def _from_der(data: bytes) -> ec.EllipticCurvePublicKey:
    loaded = serialization.load_der_public_key(bytes(data))
    if not isinstance(loaded, ec.EllipticCurvePublicKey):
        raise ValueError("credential is not an EC public key")
    return loaded


def _coerce_public_key(public_key: Union[Any, Mapping, bytes, str]) -> ec.EllipticCurvePublicKey:
    """Accept a key object, a PEM string, base64 of a DER, or a COSE_Key map.

    The base64 form matters for the browser: ``getPublicKey()`` hands back a
    SubjectPublicKeyInfo DER as an ``ArrayBuffer``, and stringifying that is
    the shortest path to something JSON can carry. Without it, a frontend has
    to re-wrap the DER in PEM headers itself.
    """
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        return public_key
    if isinstance(public_key, Mapping):
        return cose_key_to_public_key(public_key)
    if isinstance(public_key, str) and "-----BEGIN" in public_key:
        loaded = serialization.load_pem_public_key(public_key.encode("ascii"))
        if not isinstance(loaded, ec.EllipticCurvePublicKey):
            raise ValueError("credential is not an EC public key")
        return loaded
    if isinstance(public_key, str):
        padded = public_key.strip().replace("-", "+").replace("_", "/")
        padded += "=" * (-len(padded) % 4)
        try:
            return _from_der(base64.b64decode(padded, validate=True))
        except Exception:  # noqa: BLE001 - any decode/parse failure is "not a key"
            raise ValueError(
                "not a PEM public key, and not base64/base64url of a DER one"
            ) from None
    if isinstance(public_key, (bytes, bytearray)):
        return _from_der(public_key)
    raise ValueError(f"unsupported public key type: {type(public_key).__name__}")


def coerce_public_key(public_key: Union[Any, Mapping, bytes, str]) -> ec.EllipticCurvePublicKey:
    """Public entry point: is this a usable EC public key?

    Raises :class:`ValueError` if not.
    """
    return _coerce_public_key(public_key)


def coerce_es256_public_key(
    public_key: Union[Any, Mapping, bytes, str]
) -> ec.EllipticCurvePublicKey:
    """Is this a key an ES256 signature can be verified against?

    Raises :class:`ValueError` if not. ``/api/webauthn/register`` uses it to
    refuse a key it will never be able to verify a signature against, instead
    of storing it and failing later with ``signature_invalid`` — a failure
    that reads like a signing bug in the frontend rather than a registration
    bug.

    The curve check is the point: P-384 parses fine as "an EC public key",
    but every signature in this system is ES256, so a P-384 credential could
    register successfully and then never verify.
    """
    key = _coerce_public_key(public_key)
    if key.curve.name != ec.SECP256R1().name:
        raise ValueError(
            f"credential is on {key.curve.name}, but every signature here is "
            f"ES256, which requires {ec.SECP256R1().name}"
        )
    return key


def verify_es256(public_key: Any, signed: bytes, signature: bytes) -> bool:
    """Verify an ES256 signature over ``signed``.

    ``signed`` is ``authenticator_data ‖ SHA256(client_data_json)``
    (CONTRACT.md §5, ``signature_invalid``). Returns ``False`` for anything
    that does not verify, including a malformed signature or an unusable key.
    """
    try:
        key = _coerce_public_key(public_key)
        der = raw_signature_to_der(signature)
        key.verify(der, signed, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError, AttributeError, IndexError):
        return False
