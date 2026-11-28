"""The Compose service must pass the container every setting the shim reads from its environment.

A setting that Compose does not forward cannot be changed by a user of ``docker compose --profile doctr up``,
whatever the README says. Needs only the standard library.
"""

from __future__ import annotations

import re
from pathlib import Path

SHIM = Path(__file__).resolve().parents[1] / "doctr_shim"
COMPOSE = Path(__file__).resolve().parents[3] / "docker-compose.yml"


def shim_variables() -> set[str]:
    found: set[str] = set()
    for source in SHIM.glob("*.py"):
        found |= set(re.findall(r"DOCTR_SHIM_[A-Z_]+", source.read_text(encoding="utf-8")))
    return found


def doctr_service() -> str:
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(r"^  doctr:\n(?P<body>(?:    .*\n|\n)+)", text, re.MULTILINE)
    assert match, "no doctr service in docker-compose.yml"
    return match["body"]


def test_compose_forwards_every_variable_the_shim_reads():
    forwarded = set(re.findall(r"^\s+(DOCTR_SHIM_[A-Z_]+):", doctr_service(), re.MULTILINE))
    assert shim_variables(), "the shim reads no DOCTR_SHIM_* variable: this test would pass for nothing"
    assert sorted(shim_variables() - forwarded) == []
