import time

import pytest

from webinars.crypto import (
    decrypt_secret,
    encrypt_secret,
    generate_token,
    hash_token,
    sign_webhook,
    verify_webhook_signature,
)


def test_encrypt_decrypt_roundtrip():
    token = encrypt_secret("s3cr3t-value")
    assert token != "s3cr3t-value"
    assert "s3cr3t" not in token
    assert decrypt_secret(token) == "s3cr3t-value"


def test_encryption_is_randomized():
    assert encrypt_secret("x") != encrypt_secret("x")


def test_decrypt_with_wrong_key_fails(settings):
    token = encrypt_secret("value")
    settings.INTEGRATION_MASTER_KEY = "MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE="  # b"1"*32
    with pytest.raises(Exception):
        decrypt_secret(token)


def test_key_rotation_previous_key_decrypts(settings):
    old = settings.INTEGRATION_MASTER_KEY
    token = encrypt_secret("value")
    settings.INTEGRATION_MASTER_KEY = "MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE="
    settings.INTEGRATION_MASTER_KEY_PREVIOUS = [old]
    assert decrypt_secret(token) == "value"


def test_hash_token_is_sha256_hex():
    h = hash_token("abc")
    assert h == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert len(generate_token()) >= 32


def test_sign_and_verify():
    ts = int(time.time())
    sig = sign_webhook("secret", "delivery-1", ts, b'{"a":1}')
    assert sig.startswith("v1=")
    assert verify_webhook_signature("secret", "delivery-1", ts, b'{"a":1}', sig)


def test_stale_timestamp_rejected():
    ts = int(time.time()) - 3600
    sig = sign_webhook("secret", "delivery-1", ts, b"{}")
    assert not verify_webhook_signature("secret", "delivery-1", ts, b"{}", sig)


def test_tampered_body_rejected():
    ts = int(time.time())
    sig = sign_webhook("secret", "delivery-1", ts, b'{"amount":1}')
    assert not verify_webhook_signature("secret", "delivery-1", ts, b'{"amount":2}', sig)


def test_tampered_delivery_id_or_timestamp_rejected():
    ts = int(time.time())
    sig = sign_webhook("secret", "delivery-1", ts, b"{}")
    assert not verify_webhook_signature("secret", "delivery-2", ts, b"{}", sig)
    assert not verify_webhook_signature("secret", "delivery-1", ts - 1, b"{}", sig)
    assert not verify_webhook_signature("other", "delivery-1", ts, b"{}", sig)
