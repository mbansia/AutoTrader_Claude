"""Best-effort detection of this process's public outbound IP, used so the user can paste it
into Binance's API key whitelist. Cached for an hour so we don't hammer the lookup service."""
from __future__ import annotations

import time
import urllib.request


_PROBE_URLS = (
    'https://api.ipify.org',
    'https://ifconfig.me/ip',
    'https://icanhazip.com',
)
_CACHE_TTL_SECONDS = 3600

_cache: dict[str, float | str | None] = {'ip': None, 'error': '', 'ts': 0.0}


def get_outbound_ip(force: bool = False) -> tuple[str | None, str]:
    """Return (ip, error). Tries a couple of plain-text IP lookup services in order;
    falls back to None if all of them fail (e.g. egress firewall blocks them)."""
    now = time.time()
    cached_ip = _cache.get('ip')
    cached_ts = _cache.get('ts') or 0
    if not force and cached_ip and (now - float(cached_ts)) < _CACHE_TTL_SECONDS:
        return str(cached_ip), str(_cache.get('error') or '')

    last_err = ''
    for url in _PROBE_URLS:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                ip = resp.read().decode('utf-8').strip()
                if ip and 7 <= len(ip) <= 45:  # rough sanity for IPv4 / IPv6
                    _cache['ip'] = ip
                    _cache['error'] = ''
                    _cache['ts'] = now
                    return ip, ''
        except Exception as e:
            last_err = f'{url}: {e}'
            continue
    _cache['ip'] = None
    _cache['error'] = last_err
    _cache['ts'] = now
    return None, last_err
