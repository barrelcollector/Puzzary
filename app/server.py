"""Production entry point with validated optional TLS and proxy settings."""
from __future__ import annotations

import logging

import uvicorn

from app.transport import TransportConfigurationError, load_transport_config


def main() -> None:
    try:
        config = load_transport_config(validate_files=True)
    except TransportConfigurationError as error:
        raise SystemExit(f"Puzzary transport configuration error: {error}") from error

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("puzzary.transport")
    mode = "native HTTPS" if config.native_tls else "HTTP"
    if config.trusted_proxies:
        mode += " behind trusted proxy headers"
    logger.info("Starting Puzzary in %s mode on %s:%s", mode, config.host, config.port)
    for warning in config.warnings:
        logger.warning(warning)

    uvicorn.run(
        "app.main:app",
        host=config.host,
        port=config.port,
        proxy_headers=bool(config.trusted_proxies),
        forwarded_allow_ips=config.forwarded_allow_ips,
        ssl_certfile=config.tls_cert_file or None,
        ssl_keyfile=config.tls_key_file or None,
        ssl_keyfile_password=config.tls_key_password or None,
    )


if __name__ == "__main__":
    main()
