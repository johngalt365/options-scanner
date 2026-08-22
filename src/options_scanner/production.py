"""Fail-closed WSGI application imported by Gunicorn in production."""

from __future__ import annotations

from urllib.parse import urlsplit

from options_scanner.private_beta_entrypoint import ConfigurationError, PrivateBetaConfig, build_application


class LocalProxyMiddleware:
    """Accept forwarding metadata only from the loopback reverse proxy."""

    def __init__(self, app, public_url: str):
        self.app = app
        parsed = urlsplit(public_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
            raise ConfigurationError("OPTIONS_SCANNER_PUBLIC_URL debe ser un origen HTTPS sin path.")
        self.public_host = parsed.netloc

    def __call__(self, environ, start_response):
        if environ.get("REMOTE_ADDR") not in {"127.0.0.1", "::1"}:
            for key in ("HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_PROTO", "HTTP_X_FORWARDED_HOST", "HTTP_X_REQUEST_ID"):
                environ.pop(key, None)
        else:
            forwarded_proto = environ.get("HTTP_X_FORWARDED_PROTO", "").split(",", 1)[0].strip()
            forwarded_host = environ.get("HTTP_X_FORWARDED_HOST", "").split(",", 1)[0].strip()
            has_forwarding = bool(forwarded_proto or forwarded_host or environ.get("HTTP_X_FORWARDED_FOR"))
            if has_forwarding and (forwarded_proto != "https" or forwarded_host != self.public_host):
                body = b"Invalid proxy metadata"
                start_response("400 Bad Request", [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
                return [body]
            if has_forwarding:
                environ["wsgi.url_scheme"] = "https"
                environ["HTTP_HOST"] = forwarded_host
        return self.app(environ, start_response)


config = PrivateBetaConfig.from_environ()
if config.environment != "production":
    raise ConfigurationError("El entrypoint WSGI exige OPTIONS_SCANNER_ENV=production.")
application = LocalProxyMiddleware(build_application(config), config.public_url)
