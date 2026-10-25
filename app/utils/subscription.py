"""Shared absolute subscription URLs for the API, dashboard, Telegram and CLI."""

import ipaddress
import secrets

from config import (UVICORN_HOST, UVICORN_PORT, UVICORN_SSL_CERTFILE,
                    UVICORN_SSL_KEYFILE, XRAY_SUBSCRIPTION_PATH,
                    XRAY_SUBSCRIPTION_URL_PREFIX)


def get_subscription_url_prefix() -> str:
    prefix = XRAY_SUBSCRIPTION_URL_PREFIX.strip().rstrip("/")
    if prefix:
        return prefix.replace("*", secrets.token_hex(8))

    try:
        address = ipaddress.ip_address(UVICORN_HOST.strip().strip("[]"))
    except ValueError:
        address = None

    if address is None or address.is_unspecified:
        # Reuse the public IP already detected by subscription sharing. Do not
        # make network requests each time a user is serialized.
        from app.subscription.share import SERVER_IP

        try:
            address = ipaddress.ip_address((SERVER_IP or "").strip().strip("[]"))
        except ValueError:
            address = ipaddress.ip_address("127.0.0.1")
        if address.is_unspecified:
            address = ipaddress.ip_address("127.0.0.1")

    host = f"[{address}]" if address.version == 6 else str(address)
    scheme = "https" if UVICORN_SSL_CERTFILE and UVICORN_SSL_KEYFILE else "http"
    return f"{scheme}://{host}:{UVICORN_PORT}"


def get_subscription_url(token: str) -> str:
    return f"{get_subscription_url_prefix()}/{XRAY_SUBSCRIPTION_PATH}/{token}"
