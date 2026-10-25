import click
import logging
import os
import ssl

import uvicorn
from cryptography import x509
from cryptography.hazmat.backends import default_backend

from app import app, logger
from config import (DEBUG, UVICORN_HOST, UVICORN_PORT, UVICORN_SSL_CERTFILE,
                    UVICORN_SSL_KEYFILE, UVICORN_SSL_CA_TYPE, UVICORN_UDS)


def validate_cert_and_key(cert_file_path, key_file_path, ca_type):
    if ca_type == "private":
        logger.warning(f"""
{click.style('IMPORTANT!', blink=True, bold=True, fg="yellow")} 
You're running Marzban with: {click.style('UVICORN_SSL_CA_TYPE', italic=True, fg="magenta")}: {click.style(f'{ca_type}', bold=True, fg="yellow")}. 
Self-signed CAs are useful in testing or internal use cases, they’re not suitable for secure public internet communications.
        """)
        return

    if not os.path.isfile(cert_file_path):
        raise ValueError(f"SSL certificate file '{cert_file_path}' does not exist.")
    if not os.path.isfile(key_file_path):
        raise ValueError(f"SSL key file '{key_file_path}' does not exist.")

    try:
        context = ssl.create_default_context()
        context.load_cert_chain(certfile=cert_file_path, keyfile=key_file_path)
    except ssl.SSLError as e:
        raise ValueError(f"SSL Error: {e}")

    try:
        with open(cert_file_path, 'rb') as cert_file:
            cert_data = cert_file.read()
            cert = x509.load_pem_x509_certificate(cert_data, default_backend())

        if cert.issuer == cert.subject:
            raise ValueError("The certificate is self-signed and not issued by a trusted CA.")

    except Exception as e:
        raise ValueError(f"Certificate verification failed: {e}")


def get_bind_args():
    if UVICORN_UDS:
        bind_args = {'uds': UVICORN_UDS}
    else:
        bind_args = {'host': UVICORN_HOST, 'port': UVICORN_PORT}

    if UVICORN_SSL_CERTFILE and UVICORN_SSL_KEYFILE:
        ca_type = UVICORN_SSL_CA_TYPE if UVICORN_SSL_CA_TYPE in ('public', 'private') else 'public'
        validate_cert_and_key(UVICORN_SSL_CERTFILE, UVICORN_SSL_KEYFILE, ca_type)
        bind_args['ssl_certfile'] = UVICORN_SSL_CERTFILE
        bind_args['ssl_keyfile'] = UVICORN_SSL_KEYFILE
    else:
        if UVICORN_SSL_CERTFILE or UVICORN_SSL_KEYFILE:
            logger.warning(
                "Both UVICORN_SSL_CERTFILE and UVICORN_SSL_KEYFILE are required for HTTPS; "
                "using HTTP and ignoring the incomplete SSL configuration."
            )
        logger.warning("HTTP does not encrypt credentials or subscription links.")

    if DEBUG:
        bind_args['uds'] = None
        bind_args['host'] = '0.0.0.0'
        bind_args['port'] = UVICORN_PORT

    return bind_args


if __name__ == "__main__":
    # Do NOT change workers count for now
    # multi-workers support isn't implemented yet for APScheduler and XRay module
    bind_args = get_bind_args()

    try:
        uvicorn.run(
            "main:app",
            **bind_args,
            workers=1,
            reload=DEBUG,
            log_level=logging.DEBUG if DEBUG else logging.INFO
        )
    except FileNotFoundError:  # to prevent error on removing unix sock
        pass
