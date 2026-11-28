"""Run the live OCR probes against engines that are already serving at URLs, and calibrate their speed.

    uv run python tests/ocr_live/runner.py --engine docling=http://127.0.0.1:8701 --tier B --report out.json
    uv run python tests/ocr_live/runner.py calibrate --engine docling=http://127.0.0.1:8701 --report cal.json
    uv run python tests/ocr_live/runner.py --engine docling=http://127.0.0.1:8701 --list     # no engine needed

``--engine name=URL`` may repeat. ``name`` selects ``probes/<name>.py`` (that engine's probes) and always
``probes/baseline.py``. The runner never starts an engine: the URL can be a local process, a container or another
machine. For a key set ``OCR_LIVE_API_KEY_<NAME>`` (the name in upper case, other characters as ``_``).

A probe ends in one outcome: ``pass``, ``fail``, ``record`` (a ``gate="record"`` probe: numbers kept, never fails
the run), ``skipped`` (a ``requires`` entry is unmet) or ``error`` (the call or the predicate raised). A reply other
than 200 fails a predicate probe. Exit status: 0 every gate probe passed, 1 a gate probe failed or errored, 2 a usage
error or an engine that cannot be reached.
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # probe modules import ``probe`` and ``predicates`` by their flat names
    sys.path.insert(0, str(HERE))

from client import EngineClient, Reply  # noqa: E402
from probe import (  # noqa: E402
    TIERS,
    FixtureTruth,
    Observation,
    Probe,
    load_truth,
    runs_at,
    unmet_requirements,
)

PROBES_DIR = HERE / "probes"
REPORT_VERSION = 1
COMMANDS = ("run", "calibrate")
RESERVED_NAMES = frozenset({"baseline"})
ENGINE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
CALIBRATION_FIXTURE = "scan_clean.png"
OUTCOMES = ("pass", "fail", "record", "skipped", "error")


class UsageError(Exception):
    """A mistake in the command line, a probe table or the engine's address. Exit status 2."""


# --- probe tables ------------------------------------------------------------------------------


def probe_files(probes_dir: Path = PROBES_DIR) -> list[Path]:
    return sorted(p for p in probes_dir.glob("*.py") if not p.name.startswith("_"))


def load_probe_file(path: Path, truth: Mapping[str, FixtureTruth]) -> list[Probe]:
    """The ``PROBES`` of one module, checked: ``baseline.py`` holds ``engine="*"`` probes, ``<name>.py`` holds
    ``engine="<name>"`` probes, and every fixture is in ``truth.json`` and on disk."""
    module_name = "ocr_live_probes_" + re.sub(r"\W", "_", path.stem)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise UsageError(f"{path.name}: cannot load")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        del sys.modules[module_name]
        raise UsageError(f"{path.name}: {type(error).__name__}: {error}") from error
    probes = getattr(module, "PROBES", None)
    if not isinstance(probes, list) or not all(isinstance(p, Probe) for p in probes):
        raise UsageError(f"{path.name}: PROBES must be a list of Probe")
    expected = "*" if path.stem == "baseline" else path.stem
    for probe in probes:
        if probe.engine != expected:
            raise UsageError(f"{path.name}: probe {probe.id} has engine {probe.engine!r}, expected {expected!r}")
        fixture = truth.get(probe.fixture)
        if fixture is None or not fixture.path.is_file():
            raise UsageError(f"{path.name}: probe {probe.id} uses fixture {probe.fixture!r}, which is not available")
    return probes


def load_probes(
    engine: str, truth: Mapping[str, FixtureTruth], probes_dir: Path = PROBES_DIR
) -> tuple[list[Probe], list[str]]:
    """``baseline.py`` then ``<engine>.py`` when it exists. Also returns the module files that were read."""
    files = [probes_dir / "baseline.py"]
    if (probes_dir / f"{engine}.py").is_file():
        files.append(probes_dir / f"{engine}.py")
    probes: list[Probe] = []
    for path in files:
        probes.extend(load_probe_file(path, truth))
    seen: set[str] = set()
    for probe in probes:
        if probe.id in seen:
            raise UsageError(f"probe id {probe.id!r} is defined twice for engine {engine!r}")
        seen.add(probe.id)
    return probes, [p.name for p in files]


# --- running one engine -------------------------------------------------------------------------


def _error_text(reply: Reply) -> str:
    """The status of a reply and, for an error reply, its code and message. A parse result is not repeated."""
    error = reply.body.get("error") if isinstance(reply.body, dict) else None
    if isinstance(error, dict):
        return f"{reply.status} {error.get('code', '')} {error.get('message', '')}".strip()[:200]
    return str(reply.status) if reply.status == 200 else f"{reply.status} {str(reply.body)[:150]}"


def _outcome(gate: str, met: bool) -> str:
    return "record" if gate == "record" else ("pass" if met else "fail")


def _brief(entry: Mapping[str, Any]) -> str:
    if entry["outcome"] in ("fail", "error", "skipped") or (entry["outcome"] == "record" and not entry["met"]):
        return str(entry["detail"])
    numbers = " ".join(f"{k}={v}" for k, v in entry.get("numbers", {}).items())
    return numbers[:70]


def execute(
    probe: Probe,
    client: EngineClient,
    truth: FixtureTruth,
    info: Mapping[str, Any],
    use_async: bool,
    cache: dict[tuple[str, str], Reply],
) -> dict[str, Any]:
    """Run one probe. Replies for the same fixture and options are shared across probes of a run, so a slow engine
    parses ``scan_clean.png`` with default options once; ``seconds`` counts only the replies this probe waited for."""
    replies: list[dict[str, Any]] = []

    def call(role: str, options: Mapping[str, Any]) -> Reply:
        key = (probe.fixture, json.dumps(dict(options), sort_keys=True))
        cached = key in cache
        if not cached:
            cache[key] = client.parse(truth.path, options, use_async=use_async)
        reply = cache[key]
        replies.append(
            {
                "role": role,
                "status": reply.status,
                "seconds": round(reply.seconds, 3),
                "mode": reply.mode,
                "cached": cached,
            }
        )
        return reply

    entry: dict[str, Any] = {
        "id": probe.id,
        "fixture": probe.fixture,
        "gate": probe.gate,
        "tier": probe.tier,
        "paired": probe.paired,
        "options": dict(probe.options),
        "control": None if probe.control is None else dict(probe.control),
    }
    numbers: dict[str, Any] = {}
    try:
        run = call("run", probe.options)
        if probe.expect_status is not None:
            met = run.status == probe.expect_status and (
                probe.expect_code is None or run.error_code == probe.expect_code
            )
            want = f"{probe.expect_status} {probe.expect_code or ''}".strip()
            detail = f"answered {_error_text(run)}, expected {want}" if not met else f"answered {want}"
            outcome = _outcome(probe.gate, met)
        else:
            control = call("control", probe.control) if probe.control is not None else None
            bad = next((r for r in (run, control) if r is not None and r.status != 200), None)
            if bad is not None:
                met, detail = False, f"{'control' if bad is control else 'run'} answered {_error_text(bad)}"
            else:
                assert probe.predicate is not None
                verdict = probe.predicate(Observation(run=run, control=control, truth=truth, info=info))
                met, detail, numbers = verdict.ok, verdict.detail, dict(verdict.numbers)
            outcome = _outcome(probe.gate, met)
    except Exception as error:  # the call (timeout, connection) or the predicate raised
        met, outcome, detail = False, "error", f"{type(error).__name__}: {error}"[:300]
    entry.update(
        outcome=outcome,
        met=met,
        detail=detail,
        numbers=numbers,
        seconds=round(sum(r["seconds"] for r in replies if not r["cached"]), 3),
        replies=replies,
    )
    return entry


def run_engine(
    name: str,
    client: EngineClient,
    probes: Sequence[Probe],
    tier: str,
    *,
    info: Mapping[str, Any],
    truth: Mapping[str, FixtureTruth] | None = None,
    assumed: frozenset[str] = frozenset(),
    only: Sequence[str] = (),
    say: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run ``probes`` up to ``tier`` (``only`` keeps the ids that match one of its globs) and return the report of
    this engine: its ``/info``, the outcome of every probe, and a summary."""
    truth = truth if truth is not None else load_truth()
    use_async = bool((info.get("capabilities") or {}).get("async"))
    selected = [p for p in probes if runs_at(p, tier) and (not only or any(fnmatch.fnmatch(p.id, g) for g in only))]
    cache: dict[tuple[str, str], Reply] = {}
    entries: list[dict[str, Any]] = []
    started = time.perf_counter()
    for probe in selected:
        unmet = unmet_requirements(probe.requires, info, assumed)
        if unmet:
            entry = {
                "id": probe.id,
                "fixture": probe.fixture,
                "gate": probe.gate,
                "tier": probe.tier,
                "paired": probe.paired,
                "outcome": "skipped",
                "met": None,
                "detail": "needs " + ", ".join(unmet),
                "numbers": {},
                "seconds": 0.0,
                "replies": [],
            }
        else:
            entry = execute(probe, client, truth[probe.fixture], info, use_async, cache)
        entries.append(entry)
        if say:
            say(f"  {entry['outcome']:<7} {entry['id']:<36} {entry['seconds']:>7.2f}s  {_brief(entry)}")
    counts = {o: sum(1 for e in entries if e["outcome"] == o) for o in OUTCOMES}
    failures = [e["id"] for e in entries if e["gate"] == "gate" and e["outcome"] in ("fail", "error")]
    return {
        "engine": name,
        "engine_version": info.get("version"),
        "info": dict(info),
        "tier": tier,
        "probes": entries,
        "summary": {
            **counts,
            "total": len(entries),
            "not_run_by_tier_or_filter": len(probes) - len(selected),
            "seconds": round(time.perf_counter() - started, 3),
            "failed_gates": failures,
            "ok": not failures,
        },
    }


def calibrate_engine(
    client: EngineClient,
    truth: Mapping[str, FixtureTruth],
    *,
    info: Mapping[str, Any],
    fixture: str = CALIBRATION_FIXTURE,
    runs: int = 3,
) -> dict[str, Any]:
    """Parse ``fixture`` ``runs`` times with default options. The first run is cold (it may load models), the rest
    are warm. Seconds per page are the numbers every later unit compares against."""
    target = truth[fixture]
    use_async = bool((info.get("capabilities") or {}).get("async"))
    seconds: list[float] = []
    result: dict[str, Any] = {"fixture": fixture, "pages": len(target.pages), "runs": runs}
    for _ in range(runs):
        try:
            reply = client.parse(target.path, {}, use_async=use_async)
        except Exception as error:
            return {**result, "ok": False, "detail": f"{type(error).__name__}: {error}"[:300]}
        if reply.status != 200:
            return {**result, "ok": False, "detail": f"answered {_error_text(reply)}"}
        seconds.append(reply.seconds)
    cold, warm = seconds[0], statistics.median(seconds[1:])
    pages = len(target.pages)
    return {
        **result,
        "ok": True,
        "seconds": [round(s, 3) for s in seconds],
        "cold_seconds": round(cold, 3),
        "warm_seconds": round(warm, 3),
        "cold_seconds_per_page": round(cold / pages, 3),
        "warm_seconds_per_page": round(warm / pages, 3),
    }


# --- the machine and the command line -----------------------------------------------------------


def _cpu_name() -> str | None:
    with suppress(Exception):
        if platform.system() == "Darwin":
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=5
            )
            return out.stdout.strip() or None
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.partition(":")[2].strip()
    return None


def machine() -> dict[str, Any]:
    """What the timings depend on. No host name and no user name."""
    memory_gb = None
    with suppress(ValueError, OSError, AttributeError):
        memory_gb = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    return {
        "system": platform.system(),
        "release": platform.release(),
        "arch": platform.machine(),
        "cpu": _cpu_name(),
        "cpu_count": os.cpu_count(),
        "memory_gb": memory_gb,
        "python": platform.python_version(),
    }


def parse_engines(specs: Sequence[str], engine_filter: str | None) -> list[tuple[str, str]]:
    engines: list[tuple[str, str]] = []
    for spec in specs:
        name, sep, url = spec.partition("=")
        if not sep or not url.startswith(("http://", "https://")):
            raise UsageError(f"--engine {spec!r}: use name=URL, for example docling=http://127.0.0.1:8701")
        if not ENGINE_NAME.fullmatch(name) or name in RESERVED_NAMES:
            raise UsageError(f"--engine {spec!r}: the name must be letters, digits, '_', '.' or '-' and not 'baseline'")
        if any(name == existing for existing, _ in engines):
            raise UsageError(f"--engine {name}: given twice")
        engines.append((name, url))
    if engine_filter:
        wanted = [n.strip() for n in engine_filter.split(",") if n.strip()]
        unknown = [n for n in wanted if n not in {name for name, _ in engines}]
        if unknown:
            raise UsageError(f"--engine-filter names {', '.join(unknown)}, which --engine did not declare")
        engines = [(name, url) for name, url in engines if name in wanted]
    if not engines:
        raise UsageError("no engine to run: give --engine name=URL")
    return engines


def key_variable(name: str) -> str:
    """The environment variable that holds the API key of engine ``name``."""
    return "OCR_LIVE_API_KEY_" + re.sub(r"\W", "_", name).upper()


def connect(name: str, url: str) -> tuple[EngineClient, dict[str, Any]]:
    client = EngineClient(url, api_key=os.environ.get(key_variable(name)))
    try:
        reply = client.info()
    except httpx.HTTPError as error:
        client.close()
        raise UsageError(f"{name}: cannot reach {url}: {type(error).__name__}: {error}") from error
    if reply.status != 200 or not isinstance(reply.body, dict):
        client.close()
        raise UsageError(f"{name}: GET /info at {url} answered {reply.status}; is it a ragcentre.parser/v1 engine?")
    return client, reply.body


def build_parser(command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"runner.py {command}" if command != "run" else "runner.py",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--engine", action="append", default=[], metavar="NAME=URL", help="an engine to run; repeatable"
    )
    parser.add_argument("--engine-filter", metavar="A,B", help="run only these of the engines given")
    parser.add_argument("--report", metavar="FILE", help="write the JSON report here")
    if command == "run":
        parser.add_argument("--tier", choices=TIERS, default="B", help="run probes up to this tier (default B)")
        parser.add_argument("--probe", action="append", default=[], metavar="GLOB", help="only probes whose id matches")
        parser.add_argument("--has", action="append", default=[], metavar="REQ", help="assume a requirement is met")
        parser.add_argument("--list", action="store_true", help="list the probes that would run and exit")
    else:
        parser.add_argument("--fixture", default=CALIBRATION_FIXTURE, help="fixture to time (default scan_clean.png)")
        parser.add_argument("--runs", type=int, default=3, help="parses to time, the first is cold (default 3)")
    return parser


def _write_report(path: str | None, report: Mapping[str, Any]) -> None:
    if path:
        Path(path).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"report: {path}")


def run_command(args: argparse.Namespace) -> int:
    engines = parse_engines(args.engine, args.engine_filter)
    truth = load_truth()
    tables = {name: load_probes(name, truth) for name, _ in engines}
    if args.list:
        for name, (probes, files) in tables.items():
            print(f"{name}: {' + '.join(files)}")
            for p in probes:
                paired = "paired" if p.paired else "      "
                print(f"  tier {p.tier}  {p.gate:<6} {paired}  {p.id:<36} {p.fixture}")
        return 0
    if args.probe and not any(
        runs_at(p, args.tier) and any(fnmatch.fnmatch(p.id, g) for g in args.probe)
        for probes, _ in tables.values()
        for p in probes
    ):
        raise UsageError(f"--probe {' '.join(args.probe)}: no probe of tier {args.tier} or lower matches")
    connected = {name: connect(name, url) for name, url in engines}  # fail before any slow probe starts
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "command": "run",
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
        "tier": args.tier,
        "machine": machine(),
        "engines": {},
    }
    try:
        for name, (client, info) in connected.items():
            print(f"{name}  version {info.get('version')}  tier {args.tier}")
            report["engines"][name] = run_engine(
                name,
                client,
                tables[name][0],
                args.tier,
                info=info,
                truth=truth,
                assumed=frozenset(args.has),
                only=args.probe,
                say=print,
            )
            s = report["engines"][name]["summary"]
            print(
                f"{name}: {s['pass']} pass, {s['fail']} fail, {s['record']} record, {s['skipped']} skipped, "
                f"{s['error']} error in {s['seconds']}s"
                + (f"  FAILED GATES: {', '.join(s['failed_gates'])}" if s["failed_gates"] else "")
            )
    finally:
        for client, _ in connected.values():
            client.close()
    report["ok"] = all(e["summary"]["ok"] for e in report["engines"].values())
    _write_report(args.report, report)
    return 0 if report["ok"] else 1


def calibrate_command(args: argparse.Namespace) -> int:
    if args.runs < 2:
        raise UsageError("--runs must be at least 2: one cold run and one warm run")
    engines = parse_engines(args.engine, args.engine_filter)
    truth = load_truth()
    if args.fixture not in truth:
        raise UsageError(f"--fixture {args.fixture!r} is not in truth.json")
    connected = {name: connect(name, url) for name, url in engines}
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "command": "calibrate",
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
        "machine": machine(),
        "engines": {},
    }
    try:
        for name, (client, info) in connected.items():
            result = calibrate_engine(client, truth, info=info, fixture=args.fixture, runs=args.runs)
            report["engines"][name] = {"engine": name, "engine_version": info.get("version"), "calibration": result}
            if result["ok"]:
                print(
                    f"{name}  version {info.get('version')}  {args.fixture}: cold {result['cold_seconds']}s, "
                    f"warm {result['warm_seconds']}s ({result['warm_seconds_per_page']}s per page)"
                )
            else:
                print(f"{name}  calibration failed: {result['detail']}")
    finally:
        for client, _ in connected.values():
            client.close()
    report["ok"] = all(e["calibration"]["ok"] for e in report["engines"].values())
    _write_report(args.report, report)
    return 0 if report["ok"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv.pop(0) if argv and argv[0] in COMMANDS else "run"
    args = build_parser(command).parse_args(argv)  # a bad flag exits with status 2
    try:
        return run_command(args) if command == "run" else calibrate_command(args)
    except UsageError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
