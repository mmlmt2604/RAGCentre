"""The data shapes of the live OCR checks: ``Probe``, ``Check``, ``Verdict``, ``Observation`` and the fixture truth.

One table drives everything. Each row is a ``Probe``: send ``fixture`` to ``engine`` with ``options``
(and, for a paired probe, once more with ``control`` options), then hand both replies and the fixture's
ground truth to ``predicate``. A predicate never reads the engine's own output as the expected value.

Engine probe modules live in ``probes/<engine>.py`` and define ``PROBES: list[Probe]``::

    from probe import Probe
    import predicates as P

    PROBES = [
        Probe(id="docling.ocr", engine="docling", fixture="scan_multipage.pdf",
              options={"ocr": True}, control={"ocr": False},        # paired: option on against off
              predicate=P.recall_pair(treatment_min=0.8, control_max=0.1)),
        Probe(id="docling.gif", engine="docling", fixture="scan_clean.gif",
              expect_status=415, expect_code="unsupported_media_type"),   # error probe
    ]
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from client import Reply

HERE = Path(__file__).resolve().parent
FIXTURES_DIR = HERE / "fixtures"
TRUTH_JSON = HERE / "truth.json"

GATES = ("gate", "record")
# A probe runs when its tier is at or below the tier asked for: ``--tier B`` runs A and B probes.
# A: never reaches the OCR engine (contract and error answers). B: default options plus a few option probes.
# C: the full matrix.
TIERS = ("A", "B", "C")


# --- results of a check -------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """``ok`` plus the numbers that were measured, so a ``record`` probe still stores them."""

    ok: bool
    detail: str = ""
    numbers: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Check:
    """A named predicate. ``kind`` is ``structure``, ``recall``, ``geometry`` or ``compare``.

    ``recall`` checks must fail on a shim that returns unrelated text. A probe with a ``control``
    must fail on a shim that ignores its options."""

    name: str
    kind: str
    fn: Callable[[Observation], Verdict]

    def __call__(self, obs: Observation) -> Verdict:
        return self.fn(obs)


# --- fixture truth ------------------------------------------------------------------------------


@dataclass(frozen=True)
class LineTruth:
    text: str
    tokens: tuple[str, ...]
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1; origin top-left, in the page's units


@dataclass(frozen=True)
class BlockTruth:
    type: str
    text: str
    bbox: tuple[float, float, float, float]
    level: int | None = None
    html: bool = False  # a table block that an engine should give as html as well


@dataclass(frozen=True)
class PageTruth:
    page: int
    width: float
    height: float
    tokens: tuple[str, ...]
    lines: tuple[LineTruth, ...]
    blocks: tuple[BlockTruth, ...]


@dataclass(frozen=True)
class FixtureTruth:
    """What a fixture really contains. Units are pixels of the stored image for ``kind`` ``image`` and
    PDF points for ``pdf``. ``extras`` holds fixture-specific facts (table cells, formula LaTeX, ...)."""

    name: str
    kind: str
    units: str
    language: str
    sha256: str
    pages: tuple[PageTruth, ...]
    extras: Mapping[str, Any]

    @property
    def path(self) -> Path:
        return FIXTURES_DIR / self.name

    def tokens(self, pages: set[int] | None = None) -> list[str]:
        return [t for p in self.pages if pages is None or p.page in pages for t in p.tokens]


def _box(raw: list[float]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = raw
    return (float(x0), float(y0), float(x1), float(y1))


def load_truth(path: Path = TRUTH_JSON) -> dict[str, FixtureTruth]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, FixtureTruth] = {}
    for name, f in raw["fixtures"].items():
        pages = tuple(
            PageTruth(
                page=p["page"],
                width=float(p["width"]),
                height=float(p["height"]),
                tokens=tuple(p["tokens"]),
                lines=tuple(LineTruth(ln["text"], tuple(ln["tokens"]), _box(ln["bbox"])) for ln in p["lines"]),
                blocks=tuple(
                    BlockTruth(b["type"], b["text"], _box(b["bbox"]), b.get("level"), bool(b.get("html", False)))
                    for b in p["blocks"]
                ),
            )
            for p in f["pages"]
        )
        out[name] = FixtureTruth(
            name=name,
            kind=f["kind"],
            units=f["units"],
            language=f["language"],
            sha256=f["sha256"],
            pages=pages,
            extras=f.get("extras", {}),
        )
    return out


# --- what a predicate sees ----------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """The replies for one probe. ``run`` used ``options``; ``control`` (paired probes only) used ``control``."""

    run: Reply
    control: Reply | None
    truth: FixtureTruth
    info: Mapping[str, Any]


# --- the probe ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    """One row of the probe table.

    ``engine``      the engine name this probe is for, or ``*`` for any engine that takes images.
    ``fixture``     a file in ``fixtures/`` that has an entry in ``truth.json``.
    ``options``     the route options sent in the ``options`` form field. For a paired probe this is the
                    option under test (the treatment).
    ``control``     options for the control run on the same fixture, e.g. the option switched off.
                    A paired probe passes only when its predicate sees the two runs differ as predicted.
    ``predicate``   a ``Check``. Absent for an error probe.
    ``expect_status`` / ``expect_code``  the error probe form: the HTTP status (and ``error.code``) the
                    engine must answer with. A probe has a predicate or an expected status, never both.
    ``gate``        ``gate`` fails the run, ``record`` only stores its numbers.
    ``tier``        the lowest tier that runs it (see ``TIERS``).
    ``requires``    capabilities the run needs, or the probe is skipped: ``bin:<program>``, ``env:<NAME>``,
                    ``platform:<darwin|linux|windows>``, ``capability:<key>`` (truthy in ``/info.capabilities``).
                    ``bin:`` and ``env:`` look at the machine that runs the runner. ``--has`` on the
                    command line asserts a requirement that is met elsewhere (a remote engine).
    """

    id: str
    engine: str
    fixture: str
    predicate: Check | None = None
    options: Mapping[str, Any] = field(default_factory=dict)
    control: Mapping[str, Any] | None = None
    gate: str = "gate"
    tier: str = "B"
    requires: tuple[str, ...] = ()
    expect_status: int | None = None
    expect_code: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.gate not in GATES:
            raise ValueError(f"probe {self.id}: gate must be one of {GATES}, not {self.gate!r}")
        if self.tier not in TIERS:
            raise ValueError(f"probe {self.id}: tier must be one of {TIERS}, not {self.tier!r}")
        if (self.predicate is None) == (self.expect_status is None):
            raise ValueError(f"probe {self.id}: give a predicate or expect_status, not both and not neither")
        if self.control is not None and self.predicate is None:
            raise ValueError(f"probe {self.id}: a paired probe needs a predicate")
        if self.expect_code is not None and self.expect_status is None:
            raise ValueError(f"probe {self.id}: expect_code needs expect_status")

    @property
    def paired(self) -> bool:
        return self.control is not None


def runs_at(probe: Probe, tier: str) -> bool:
    return TIERS.index(probe.tier) <= TIERS.index(tier)


def unmet_requirements(
    requires: tuple[str, ...], info: Mapping[str, Any], assumed: frozenset[str] = frozenset()
) -> list[str]:
    """The entries of ``requires`` this run cannot meet. ``assumed`` holds ``--has`` values."""
    unmet: list[str] = []
    for item in requires:
        if item in assumed:
            continue
        kind, _, value = item.partition(":")
        if kind == "bin":
            met = shutil.which(value) is not None
        elif kind == "env":
            met = bool(os.environ.get(value))
        elif kind == "platform":
            met = platform.system().lower() == value.lower()
        elif kind == "capability":
            met = bool((info.get("capabilities") or {}).get(value))
        else:
            raise ValueError(f"unknown requirement {item!r}; use bin:, env:, platform: or capability:")
        if not met:
            unmet.append(item)
    return unmet
