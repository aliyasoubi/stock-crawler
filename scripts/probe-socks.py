#!/usr/bin/env python3
"""One explicit SOCKS request; no SQL access, state changes, or retries."""
import argparse
import socket
import sys
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy', default='socks5h://127.0.0.1:10808')
    args = parser.parse_args()
    try:
        parsed = urlsplit(args.proxy)
        host, port = parsed.hostname, parsed.port
        if parsed.scheme not in {'socks5', 'socks5h'} or not host or not port:
            raise ValueError()
    except ValueError:
        print('CONFIG FAIL: provide a socks5h://HOST:PORT proxy URL (credentials are not printed).')
        return 2
    try:
        import httpx
        import socksio  # noqa: F401
    except ImportError:
        print('DEPENDENCY FAIL: use the crawler image or install its httpx[socks] dependency.')
        return 2
    print(f'PROXY {parsed.scheme}://{host}:{port}')
    try:
        addresses = sorted({result[4][0] for result in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        print('PROXY DNS OK:', ', '.join(addresses))
    except socket.gaierror:
        print('PROXY DNS FAIL: check host.docker.internal mapping, or use host networking with 127.0.0.1.')
        return 2
    try:
        with socket.create_connection((host, port), timeout=5):
            print('PROXY TCP OK')
    except OSError as exc:
        print(f'PROXY TCP FAIL: {type(exc).__name__}. Check the SSH listener address and firewall.')
        return 2
    try:
        # Ignore ambient proxy variables; retain normal TLS certificate verification.
        with httpx.Client(proxy=args.proxy, trust_env=False, timeout=20, follow_redirects=False) as client:
            response = client.get('https://www.kap.org.tr/en')
    except (httpx.HTTPError, ValueError) as exc:
        print(f'KAP REQUEST FAIL: {type(exc).__name__}. TCP to the proxy worked; check SOCKS, tunnel egress and TLS.')
        return 2
    print(f'KAP HTTP {response.status_code}; Content-Type: {response.headers.get("content-type", "unknown")}')
    print('An HTTP response proves transport for this request, not that the export API will accept crawling.')
    if response.status_code in {401, 403, 429}:
        print('ACCESS/THROTTLE RESPONSE: stop here; do not repeatedly retry.')
        return 3
    if response.status_code >= 400:
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
