#!/usr/bin/env python3
"""Restricted SSH forced-command HTTPS reader for curated research feeds."""
import gzip
import ipaddress
import json
import resource
import signal
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_BYTES = 16 * 1024 * 1024
CONFIG = Path('/etc/fastnews-relay/allowlist.json')


def validate_url(value, allowed, resolve=True):
    u = urllib.parse.urlsplit(value)
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.port not in (None, 443) or u.fragment:
        raise ValueError('URL rejected')
    key = (u.hostname.lower(), u.path or '/')
    if key not in {(urllib.parse.urlsplit(x).hostname.lower(), urllib.parse.urlsplit(x).path or '/') for x in allowed}:
        raise ValueError('URL not allowlisted')
    if resolve:
        addresses = socket.getaddrinfo(u.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError('Non-public destination')
    return value


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed):
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, self.allowed)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, allowed):
    validate_url(url, allowed)
    # Ignore proxy environment inherited by sshd. This is a direct HTTPS fetch,
    # never a generic CONNECT/SOCKS proxy or a shell command supplied by a caller.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), SafeRedirect(allowed))
    request = urllib.request.Request(url, headers={'User-Agent': 'FastNewsBot/1.0 (+https://github.com/FastR-D/FastNews)'})
    with opener.open(request, timeout=45) as response:
        if response.status != 200:
            raise ValueError('Unexpected response')
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Source too large')
    if raw.startswith(b'\x1f\x8b'):
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Source too large')
    return raw


def main():
    signal.alarm(90)
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    raw = sys.stdin.buffer.read(8193)
    if len(raw) > 8192:
        raise ValueError('Request too large')
    request = json.loads(raw)
    if set(request) != {'url'} or not isinstance(request['url'], str):
        raise ValueError('Invalid request')
    data = fetch(request['url'], json.loads(CONFIG.read_text()))
    sys.stdout.buffer.write(data)


if __name__ == '__main__':
    try:
        main()
    except urllib.error.HTTPError as error:
        raise SystemExit(f'Upstream HTTP {error.code}')
    except Exception:
        # URLs, response bodies and environment are never returned in diagnostics.
        raise SystemExit('Relay fetch rejected or unavailable')
