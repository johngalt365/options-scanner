"""Gunicorn settings; values intentionally come from the service environment."""
import os

workers = 1
bind = f"127.0.0.1:{int(os.environ.get('OPTIONS_SCANNER_PORT', '8000'))}"
timeout = int(os.environ.get("OPTIONS_SCANNER_GUNICORN_TIMEOUT", "120"))
graceful_timeout = int(os.environ.get("OPTIONS_SCANNER_GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = 5
accesslog = "-"
errorlog = "-"
capture_output = True
