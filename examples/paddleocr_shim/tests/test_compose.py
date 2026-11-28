"""Compose forwards the settings the PaddleOCR shim reads."""

import re
from pathlib import Path


def test_compose_forwards_every_variable_the_shim_reads():
    root = Path(__file__).resolve().parents[3]
    variable = re.compile(r"\b(?:PADDLEOCR_SHIM|PADDLEOCR_VL|PADDLEX_SERVING)(?:_[A-Z]+)+\b")
    read = set()
    for source in (Path(__file__).resolve().parents[1] / "paddleocr_shim").glob("*.py"):
        read.update(variable.findall(source.read_text()))
    service = re.search(
        r"^  paddleocr:\n(?P<body>(?:    .*\n|\n)+)", (root / "docker-compose.yml").read_text(), re.MULTILINE
    )
    assert service
    forwarded = set(re.findall(r"^\s+((?:PADDLEOCR|PADDLEX)_[A-Z_]+):", service["body"], re.MULTILINE))
    assert {"PADDLEOCR_VL_BACKEND", "PADDLEX_SERVING_URL"} <= read
    assert sorted(read - forwarded) == []
