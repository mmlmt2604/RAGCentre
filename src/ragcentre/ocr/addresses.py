"""Where to look for parser shims.

Only known addresses are tried: the ports the example shims use, plus what the operator lists in
``RAGCENTRE_DISCOVERY_URLS``. Nothing scans a network or talks to Docker.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("ragcentre.ocr")

# The Compose service names and ports of examples/*_shim (and of shimkit's Tesseract example).
KNOWN_SHIMS = (
    ("docling", 8701),
    ("mineru", 8702),
    ("paddleocr", 8703),
    ("surya", 8704),
    ("easyocr", 8705),
    ("doctr", 8706),
    ("tesseract", 8710),
)
DOCKER_HOST = "host.docker.internal"
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
# Different names by which one service on this machine can be reached.
LOCAL_ALIASES = LOOPBACK | {DOCKER_HOST} | {name for name, _ in KNOWN_SHIMS}


def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def split_url(url: str) -> tuple[str, int] | None:
    """``(host, port)`` of an HTTP URL, or ``None`` when it is not one."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    return host, port or (443 if parts.scheme == "https" else 80)


def candidate_urls(setting: str, *, container: bool) -> list[str]:
    """The addresses to try, best first.

    ``setting`` is ``auto``, ``off``, or comma-separated URLs; include ``auto`` in the list to keep
    the known addresses as well. Inside a container loopback is the container itself, so the
    Compose service names and the Docker host are tried instead.
    """
    items = [item.strip() for item in setting.split(",") if item.strip()]
    if "off" in items:
        return []
    urls: list[str] = []
    for item in items:
        if item == "auto":
            if container:
                urls += [f"http://{name}:{port}" for name, port in KNOWN_SHIMS]
                urls += [f"http://{DOCKER_HOST}:{port}" for _, port in KNOWN_SHIMS]
            else:
                urls += [f"http://127.0.0.1:{port}" for _, port in KNOWN_SHIMS]
        elif split_url(item) is None or "@" in item:
            # Discovered URLs are shown to every API client, so credentials in one are refused.
            log.warning("ignoring an invalid entry in RAGCENTRE_DISCOVERY_URLS")
        else:
            urls.append(item.rstrip("/"))
    return list(dict.fromkeys(urls))


def same_service_address(a: str, b: str) -> bool:
    """Whether two URLs can be two names for one port on this machine."""
    one, two = split_url(a), split_url(b)
    if one is None or two is None or one[1] != two[1]:
        return False
    return one[0] == two[0] or (one[0] in LOCAL_ALIASES and two[0] in LOCAL_ALIASES)
