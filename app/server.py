"""Production entry point with validated optional TLS and proxy settings."""
from __future__ import annotations

import logging
import re
from copy import deepcopy

import uvicorn

from app.transport import TransportConfigurationError, load_transport_config


class FeedTokenLogFilter(logging.Filter):
    """Keep credentials and one-time tokens out of Uvicorn access logs."""

    pattern = re.compile(r"(/feeds/)[A-Za-z0-9_-]{32,128}(\.(?:atom|rss)(?:\?[^ ]*)?)")
    query_token_pattern = re.compile(
        r"(/(?:register/verify|password/setup)\?[^ ]*?\btoken=)[^& ]+",
        re.IGNORECASE,
    )

    @classmethod
    def redact(cls, value: str) -> str:
        value = cls.pattern.sub(r"\1[REDACTED]\2", value)
        return cls.query_token_pattern.sub(r"\1[REDACTED]", value)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self.redact(value) if isinstance(value, str) else value for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: self.redact(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        return True


def uvicorn_log_config() -> dict:
    config = deepcopy(uvicorn.config.LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_feed_tokens"] = {"()": FeedTokenLogFilter}
    access_handler = config.setdefault("handlers", {}).setdefault("access", {})
    access_handler.setdefault("filters", []).append("redact_feed_tokens")
    return config


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
        log_config=uvicorn_log_config(),
    )


if __name__ == "__main__":
    main()
