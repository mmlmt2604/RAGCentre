from __future__ import annotations

import runpy
from pathlib import Path

import pytest

summarize = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/verify_vector_databases.py"))["summarize"]


def test_missing_server_cannot_pass_because_other_tests_pass(tmp_path: Path) -> None:
    report = tmp_path / "tests.xml"
    report.write_text("""<testsuites><testsuite>
      <testcase classname="tests.test_m9_qdrant" name="config"/>
      <testcase classname="tests.test_qdrant_server" name="query">
        <skipped message="Qdrant server unavailable"/>
      </testcase>
    </testsuite></testsuites>""")
    result = summarize(report, ["qdrant"])
    assert result["ok"] is False
    assert result["profiles"]["qdrant"]["status"] == "blocked"
    assert result["profiles"]["qdrant"]["missing_suites"] == ["test_qdrant_server"]


def test_capability_exclusion_is_visible_and_allowed(tmp_path: Path) -> None:
    report = tmp_path / "tests.xml"
    report.write_text("""<testsuites><testsuite>
      <testcase classname="tests.test_m9_qdrant.TestQdrantContract" name="test_healthcheck"/>
      <testcase classname="tests.test_m9_qdrant.TestQdrantContract" name="test_upsert_overwrite_and_count"/>
      <testcase classname="tests.test_m9_qdrant.TestQdrantContract" name="test_query_ordering_per_metric[cosine]"/>
      <testcase classname="tests.test_qdrant_server.TestQdrantServerContract" name="test_healthcheck[http]"/>
      <testcase classname="tests.test_qdrant_server.TestQdrantServerContract" name="test_upsert_overwrite_and_count[http]"/>
      <testcase classname="tests.test_qdrant_server.TestQdrantServerContract" name="test_query_ordering_per_metric[http-cosine]"/>
      <testcase classname="tests.test_qdrant_server.TestQdrantServerContract" name="max_dim">
        <skipped message="store declares no max_dim"/>
      </testcase>
    </testsuite></testsuites>""")
    result = summarize(report, ["qdrant"])
    assert result["ok"] is True
    assert len(result["profiles"]["qdrant"]["capability_exclusions"]) == 1


@pytest.mark.parametrize("child", ["failure", "error"])
def test_failed_queries_are_never_successful_work(tmp_path: Path, child: str) -> None:
    report = tmp_path / "tests.xml"
    report.write_text(f"""<testsuites><testsuite>
      <testcase classname="tests.test_m9_qdrant" name="query"/>
      <testcase classname="tests.test_qdrant_server" name="query"><{child}/></testcase>
    </testsuite></testsuites>""")
    result = summarize(report, ["qdrant"])
    assert result["ok"] is False
    assert result["profiles"]["qdrant"]["status"] == "failed"
    assert result["profiles"]["qdrant"]["passed"] == 1


@pytest.mark.parametrize(
    ("profile", "classname"),
    [("redis", "tests.test_vs_redis"), ("mongodb", "tests.test_vs_mongodb.TestMongoDBAgainstTheFake")],
)
def test_configuration_or_fake_results_cannot_certify_live_stores(tmp_path: Path, profile: str, classname: str) -> None:
    report = tmp_path / "tests.xml"
    report.write_text(
        f'<testsuites><testsuite><testcase classname="{classname}" name="test_config"/></testsuite></testsuites>'
    )
    result = summarize(report, [profile])
    assert result["ok"] is False
    assert result["profiles"][profile]["status"] == "blocked"
    assert result["profiles"][profile]["missing_live_checks"]


def test_absent_profile_is_blocked(tmp_path: Path) -> None:
    report = tmp_path / "tests.xml"
    report.write_text("<testsuites/>")
    result = summarize(report, ["qdrant"])
    assert result["ok"] is False
    assert result["profiles"]["qdrant"]["status"] == "blocked"
