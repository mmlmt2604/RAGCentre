"""Run local adapter verification without allowing missing services to look green.

Start the services in tests/docker-compose.yml and install all package extras first.
This command never starts services, accesses cloud accounts, or changes application data.
The selected pytest suites create and remove their own test resources.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SUITES = {
    "sqlite_vec": ["test_m2_sqlite_vec", "test_v4_sqlite_vec"],
    "qdrant": ["test_m9_qdrant", "test_qdrant_server"],
    "pgvector": ["test_m9_pgvector"],
    "chroma": ["test_m9_chroma", "test_m9_chroma_server"],
    "lancedb": ["test_vs_lancedb"],
    "redis": ["test_vs_redis"],
    "weaviate": ["test_vs_weaviate"],
    "elasticsearch": ["test_vs_elasticsearch"],
    "opensearch": ["test_vs_opensearch"],
    "milvus": ["test_vs_milvus", "test_vs_milvus_filter"],
    "pinecone": ["test_vs_pinecone"],
    "mongodb": ["test_vs_mongodb"],
    "http": ["test_v1_http_store", "test_http_store_network"],
}
LIVE_CONTRACTS = {
    "sqlite_vec": ("TestSqliteVecContract", "TestSqliteVecPrefixedContract"),
    "qdrant": ("TestQdrantContract", "TestQdrantServerContract"),
    "pgvector": ("TestPgvectorContract", "TestPgvectorHalfvecContract"),
    "chroma": (
        "TestChromaContract",
        "TestChromaConfiguredContract",
        "TestChromaCloudBatchContract",
        "TestChromaServerContract",
    ),
    "lancedb": ("TestLanceDBContract",),
    "redis": ("TestRedisFlatContract", "TestRedisHnswContract", "TestRedisHnswDefaultEfContract"),
    "weaviate": ("TestWeaviateContract",),
    "elasticsearch": ("TestElasticsearchContract", "TestElasticsearchFlatContract", "TestElasticsearchInt8Contract"),
    "opensearch": ("TestOpenSearchFaissContract", "TestOpenSearchLuceneContract"),
    "milvus": ("TestMilvusContract", "TestMilvusHnswContract"),
    "pinecone": ("TestPineconeLocalContract",),
    "mongodb": ("TestMongoDBApproximateContract", "TestMongoDBExactContract", "TestMongoDBPrefixedContract"),
    "http": ("TestHttpStore", "TestHttpStoreWithLimits", "TestHttpStoreNetwork"),
}
LIVE_CHECKS = ("test_healthcheck", "test_upsert_overwrite_and_count", "test_query_ordering_per_metric")
ALLOWED_SKIPS = {
    "store declares no max_dim",
    "store supports a single metric",
    "only meaningful when the extra is absent",
}


def summarize(xml_path: Path, profiles: list[str]) -> dict[str, Any]:
    root = ET.parse(xml_path).getroot()
    results: dict[str, Any] = {}
    for profile in profiles:
        cases = [
            case
            for case in root.iter("testcase")
            if any(module in case.get("classname", "").split(".") for module in SUITES[profile])
        ]
        failures = []
        blocked = []
        exclusions = []
        passed = 0
        for case in cases:
            name = f"{case.get('classname')}.{case.get('name')}"
            failure = case.find("failure")
            error = case.find("error")
            skipped = case.find("skipped")
            if failure is not None or error is not None:
                failures.append(name)
            elif skipped is not None:
                reason = skipped.get("message", "").removeprefix("Skipped: ")
                item = {"test": name, "reason": reason}
                (exclusions if reason in ALLOWED_SKIPS else blocked).append(item)
            else:
                passed += 1
        missing = [
            module
            for module in SUITES[profile]
            if not any(
                module in case.get("classname", "").split(".") and case.find("skipped") is None for case in cases
            )
        ]
        missing_live = [
            f"{contract}.{check}"
            for contract in LIVE_CONTRACTS[profile]
            for check in LIVE_CHECKS
            if not any(
                case.get("classname", "").split(".")[-1] == contract
                and case.get("name", "").split("[", 1)[0] == check
                and all(case.find(result) is None for result in ("failure", "error", "skipped"))
                for case in cases
            )
        ]
        status = "failed" if failures else "blocked" if blocked or missing or missing_live or not passed else "passed"
        results[profile] = {
            "status": status,
            "passed": passed,
            "failures": failures,
            "blocked": blocked,
            "capability_exclusions": exclusions,
            "missing_suites": missing,
            "missing_live_checks": missing_live,
        }
    return {"ok": all(result["status"] == "passed" for result in results.values()), "profiles": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", action="append", choices=sorted(SUITES), help="Repeat to select profiles; default all"
    )
    parser.add_argument("--output", type=Path, help="New directory for local logs, JUnit XML, and JSON")
    args = parser.parse_args()
    profiles = list(dict.fromkeys(args.profile or SUITES))
    output = args.output or ROOT / "docs/plans/vector-verification-run" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, exist_ok=False)
    xml_path = output.resolve() / "pytest.xml"
    command = [sys.executable, "-m", "pytest", "-o", "addopts=", "-m", "not cloud", f"--junitxml={xml_path}"]
    command.extend(f"tests/{module}.py" for profile in profiles for module in SUITES[profile])
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env.setdefault("RAGCENTRE_TEST_PINECONE_URL", "http://localhost:55080")
    print(f"Running {', '.join(profiles)}. Evidence: {output.resolve()}", flush=True)
    try:
        with (output / "pytest.log").open("w") as log:
            process = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
        report = (
            summarize(xml_path, profiles) if xml_path.exists() else {"ok": False, "error": "pytest produced no report"}
        )
        report["pytest_exit_code"] = process.returncode
        report["ok"] = report["ok"] and process.returncode == 0
    except subprocess.TimeoutExpired:
        report = {"ok": False, "error": "verification exceeded 1800 seconds"}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
