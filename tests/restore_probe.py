"""Synthetic restore probe; run only in a disposable test environment."""
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

def main(mode):
    from cryptography.fernet import Fernet
    from django.contrib.auth import authenticate, get_user_model
    from django.test import Client, override_settings

    from webinars.crypto import DecryptionError, decrypt_secret, encrypt_secret
    from webinars.models import Destination

    secret = "synthetic-not-a-live-credential"
    password = "synthetic-restore-password-not-for-production"
    if mode == "seed":
        get_user_model().objects.create_user("restore-check", password=password, is_staff=True)
        Destination.objects.create(
            name="restore-check", url="https://example.invalid/webhook", enabled=False,
            credentials_encrypted=encrypt_secret(secret),
            signing_secret_encrypted=encrypt_secret(secret),
        )
        return
    assert mode == "verify"
    destination = Destination.objects.get(name="restore-check")
    assert not destination.enabled
    assert decrypt_secret(destination.credentials_encrypted) == secret
    assert decrypt_secret(destination.signing_secret_encrypted) == secret
    with override_settings(INTEGRATION_MASTER_KEY=Fernet.generate_key().decode(), INTEGRATION_MASTER_KEY_PREVIOUS=[]):
        try:
            decrypt_secret(destination.credentials_encrypted)
        except DecryptionError:
            pass
        else:
            raise AssertionError("Wrong master key decrypted restored credential")
    assert authenticate(username="restore-check", password=password) is not None
    with override_settings(ALLOWED_HOSTS=["testserver"], SECURE_SSL_REDIRECT=False):
        client = Client()
        assert client.login(username="restore-check", password=password)
        assert client.get("/admin/", secure=True).status_code == 200
    print("Restored app records, credential decryption, wrong-key rejection and authenticated admin request passed.")


if __name__ == "__main__":
    main(sys.argv[1])
