"""Optional Tailscale Serve access without exposing the loopback app directly."""
from email.header import decode_header, make_header
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit


def load_access(path: Path) -> dict | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"origin", "operator_login"}:
        raise ValueError("Private Studio access needs an origin and operator_login")
    origin, login = value["origin"], value["operator_login"]
    if not isinstance(origin, str) or not isinstance(login, str) or not login.strip():
        raise ValueError("Invalid private Studio access configuration")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".ts.net") or
            parsed.username or parsed.password or parsed.port is not None or parsed.path or
            parsed.query or parsed.fragment or origin != "https://" + parsed.hostname or
            any(ord(c) <= 32 or ord(c) >= 127 for c in origin) or
            any(ord(c) < 32 for c in login)):
        raise ValueError("Use the exact Tailscale HTTPS origin without a port or path")
    return {"origin": origin, "operator_login": login.strip()}


def request_origin(headers, peer: str, port: int, access: dict | None) -> str | None:
    """Trust Serve identity only over loopback; never trust forwarded Host/Origin."""
    try:
        if not ipaddress.ip_address(peer).is_loopback:
            return None
    except ValueError:
        return None
    if headers.get("Sec-Fetch-Site") == "cross-site":
        return None
    host = headers.get("Host")
    local_host = f"127.0.0.1:{port}"
    proxied = any(headers.get(name) is not None for name in
                  ("Tailscale-User-Login", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto"))
    if host == local_host and not proxied:
        return f"http://{host}"
    if not access or host not in (local_host, urlsplit(access["origin"]).netloc):
        return None
    try:
        login = str(make_header(decode_header(headers.get("Tailscale-User-Login", ""))))
    except (ValueError, UnicodeError, LookupError):
        return None
    if login.casefold() != access["operator_login"].casefold():
        return None
    return access["origin"]
