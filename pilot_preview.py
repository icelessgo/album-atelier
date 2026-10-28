"""Optional synthetic print preview over the existing owner-only Studio tunnel."""
from http.client import HTTPConnection
from urllib.parse import urlsplit

ROOTS = ("/pilot/", "/gallery/", "/print-orders/", "/admin/", "/assets/", "/locales/",
         "/fonts/", "/api/gallery/", "/api/auth/", "/api/admin/", "/api/public/",
         "/api/print-shop/", "/api/branding/")
EXACT = {"/pilot", "/admin", "/favicon.ico", "/impressum", "/datenschutz"}


def matches(path: str) -> bool:
    path = urlsplit(path).path
    return path in EXACT or path.startswith(ROOTS)


def forward(handler, method: str) -> None:
    """Called only after Studio's existing Host/peer/Tailscale owner checks."""
    if not handler.studio_origin.startswith("https://"):
        if method == "GET":
            handler.send_response(302)
            handler.send_header("Location", "http://127.0.0.1:18765" + handler.path)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        return handler._json(403, {"error": "Use the separate local print pilot address"})
    if method != "GET" and handler.headers.get("Origin") != handler.studio_origin:
        return handler._json(403, {"error": "Invalid private pilot request origin"})
    length = int(handler.headers.get("Content-Length", "0"))
    if not 0 <= length <= 2 * 1024 * 1024:
        return handler._json(413, {"error": "Pilot request is too large"})
    body = handler.rfile.read(length) if length else None
    names = ("Host", "Origin", "Cookie", "Content-Type", "Authorization", "User-Agent",
             "Tailscale-User-Login", "Sec-Fetch-Site", "Sec-Fetch-Mode", "Referer",
             "X-Print-Buyer", "X-Print-Order-Key", "Range", "If-None-Match")
    headers = {name: handler.headers[name] for name in names if handler.headers.get(name) is not None}
    connection = HTTPConnection("127.0.0.1", 18766, timeout=30)
    try:
        connection.request(method, handler.path, body, headers)
        response = connection.getresponse()
        payload = response.read(32 * 1024 * 1024 + 1)
        if len(payload) > 32 * 1024 * 1024:
            return handler._json(502, {"error": "Pilot response is too large"})
        handler.send_response(response.status)
        allowed = {"content-type", "cache-control", "x-content-type-options", "x-frame-options",
                   "referrer-policy", "content-security-policy", "location", "set-cookie",
                   "content-disposition", "content-range", "accept-ranges", "etag"}
        for name, value in response.getheaders():
            if name.lower() in allowed:
                handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
    except (ConnectionError, OSError):
        return handler._json(502, {"error": "The private print pilot is starting; try again shortly"})
    finally:
        connection.close()
