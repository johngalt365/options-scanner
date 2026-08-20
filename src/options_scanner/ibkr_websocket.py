"""Cliente WebSocket mínimo y seguro para diagnóstico ``smd`` de IBKR."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
import os
import select
import socket
import ssl
import struct
import time
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from options_scanner.ibkr import GatewayUnavailableError, _safe_market_value


SMD_FIELDS = ("31", "84", "86", "6509", "7308", "7309", "7310", "7311", "7633", "7635", "7638")


@dataclass(frozen=True, slots=True)
class StreamObservation:
    elapsed_seconds: float
    fields: Mapping[str, Any]


class WebSocketConnection(Protocol):
    def send_text(self, value: str) -> None: ...
    def receive_text(self, timeout: float) -> str | None: ...
    def close(self) -> None: ...


def parse_smd_message(message: str, conid: str) -> Mapping[str, Any]:
    """Return only allow-listed scalar market fields for the requested conid."""

    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(payload, Mapping) or payload.get("topic") != f"smd+{conid}":
        return {}
    return {
        field: _safe_market_value(payload[field])
        for field in SMD_FIELDS
        if field in payload
    }


def observe_smd_stream(
    connection: WebSocketConnection,
    conid: str,
    duration: float,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[StreamObservation, ...]:
    """Subscribe to one conid and preserve the temporal evolution of its fields."""

    duration = max(0.0, duration)
    request = json.dumps({"fields": list(SMD_FIELDS)}, separators=(",", ":"))
    connection.send_text(f"smd+{conid}+{request}")
    started = clock()
    observations: list[StreamObservation] = []
    try:
        while (remaining := duration - (clock() - started)) > 0:
            message = connection.receive_text(remaining)
            if message is None:
                break
            fields = parse_smd_message(message, conid)
            if fields:
                observations.append(StreamObservation(max(0.0, clock() - started), fields))
    finally:
        connection.send_text(f"umd+{conid}+{{}}")
        connection.close()
    return tuple(observations)


def compare_market_fields(snapshot: tuple[Any, ...], stream: tuple[StreamObservation, ...]) -> Mapping[str, tuple[str, ...]]:
    snapshot_fields = {str(field) for item in snapshot for field in item.fields}
    stream_fields = {str(field) for item in stream for field in item.fields}
    return {
        "snapshot": tuple(field for field in SMD_FIELDS if field in snapshot_fields),
        "websocket": tuple(field for field in SMD_FIELDS if field in stream_fields),
        "websocket_only": tuple(field for field in SMD_FIELDS if field in stream_fields - snapshot_fields),
        "snapshot_only": tuple(field for field in SMD_FIELDS if field in snapshot_fields - stream_fields),
    }


class ClientPortalWebSocket:
    """Small RFC 6455 text client; it never logs handshake/session material."""

    def __init__(self, base_url: str, *, allow_insecure_tls: bool = False, timeout: float = 10.0) -> None:
        parsed = urlsplit(base_url)
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        base_path = parsed.path.rstrip("/")
        self._path = f"{base_path}/ws"
        try:
            raw = socket.create_connection((self._host, self._port), timeout=timeout)
            if parsed.scheme == "https":
                context = ssl._create_unverified_context() if allow_insecure_tls else ssl.create_default_context()
                raw = context.wrap_socket(raw, server_hostname=self._host)
            self._socket = raw
            self._handshake(timeout)
        except GatewayUnavailableError:
            raise
        except (OSError, ssl.SSLError) as exc:
            raise GatewayUnavailableError("no se pudo conectar al WebSocket de Client Portal Gateway") from exc

    def _handshake(self, timeout: float) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self._path} HTTP/1.1\r\nHost: {self._host}:{self._port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._socket.sendall(request.encode("ascii"))
        self._socket.settimeout(timeout)
        response = b""
        while b"\r\n\r\n" not in response and len(response) < 16384:
            response += self._socket.recv(4096)
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        header_text = response.decode("latin-1", errors="replace")
        if not header_text.startswith("HTTP/1.1 101") or expected.lower() not in header_text.lower():
            self._socket.close()
            raise GatewayUnavailableError("Gateway rechazó la conexión WebSocket de diagnóstico")

    def send_text(self, value: str) -> None:
        payload = value.encode("utf-8")
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray((0x81, 0x80 | (length if length < 126 else 126)))
        if length >= 126:
            if length > 65535:
                raise ValueError("mensaje WebSocket demasiado largo")
            header.extend(struct.pack("!H", length))
        header.extend(mask)
        header.extend(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(header)

    def receive_text(self, timeout: float) -> str | None:
        ready, _, _ = select.select((self._socket,), (), (), max(0.0, timeout))
        if not ready:
            return None
        first = self._read_exact(2)
        opcode, length = first[0] & 0x0F, first[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        payload = self._read_exact(length)
        if opcode == 0x8:
            return None
        if opcode == 0x9:
            self._send_control(0xA, payload)
            return ""
        return payload.decode("utf-8", errors="replace") if opcode == 0x1 else ""

    def _read_exact(self, length: int) -> bytes:
        result = b""
        while len(result) < length:
            chunk = self._socket.recv(length - len(result))
            if not chunk:
                raise GatewayUnavailableError("Gateway cerró el WebSocket de diagnóstico")
            result += chunk
        return result

    def _send_control(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        frame = bytes((0x80 | opcode, 0x80 | len(payload))) + mask
        frame += bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(frame)

    def close(self) -> None:
        try:
            self._send_control(0x8, b"")
        finally:
            self._socket.close()
