"""JUnit refresh must preserve measured costs and reject misleading input."""

import json
import sys
from pathlib import Path

import pytest
from refresh_durations import main, module_paths, report_weights


def report(tmp_path, cases, *, attributes=""):
    path = tmp_path / "junit.xml"
    path.write_text(f"<testsuites><testsuite {attributes}>{cases}</testsuite></testsuites>")
    return path


def test_refresh_sums_parameterized_and_class_cases_and_preserves_unmeasured_weights(
    tmp_path, monkeypatch
):
    source = tmp_path / "tests/test_example.py"
    source.parent.mkdir()
    source.touch()
    junit = report(
        tmp_path,
        """
        <testcase classname="tests.test_example" name="test_a[one]" time="1.25"/>
        <testcase classname="tests.test_example.Nested.TestClass" name="test_b" time="2.5"/>
    """,
    )
    output = tmp_path / "weights.json"
    output.write_text(json.dumps({"unmeasured.py": 9, "tests/test_example.py": 100}))
    monkeypatch.setattr("refresh_durations.ROOT", tmp_path)
    monkeypatch.setattr(
        "refresh_durations.module_paths",
        lambda root: {"tests.test_example": "tests/test_example.py"},
    )
    monkeypatch.setattr(sys, "argv", ["refresh", str(junit), "--output", str(output)])
    main()
    assert json.loads(output.read_text()) == {"tests/test_example.py": 3.75, "unmeasured.py": 9}


@pytest.mark.parametrize(
    "case,attributes",
    [
        ('<testcase classname="tests.test_a" name="test_a" time="1"><failure/></testcase>', ""),
        ('<testcase classname="tests.test_a" name="test_a" time="1"><skipped/></testcase>', ""),
        ('<testcase classname="tests.test_a" name="test_a" time="-1"/>', ""),
        ('<testcase classname="tests.test_a" name="test_a" time="NaN"/>', ""),
        ('<testcase classname="missing.test_a" name="test_a" time="1"/>', ""),
        ('<testcase classname="tests.test_a" name="test_a" time="1"/>', 'errors="1"'),
        ("", ""),
    ],
)
def test_invalid_reports_leave_output_unchanged(tmp_path, monkeypatch, case, attributes):
    junit = report(tmp_path, case, attributes=attributes)
    output = tmp_path / "weights.json"
    original = '{"existing.py": 42}\n'
    output.write_text(original)
    monkeypatch.setattr("refresh_durations.ROOT", tmp_path)
    monkeypatch.setattr(
        "refresh_durations.module_paths", lambda root: {"tests.test_a": "tests/test_a.py"}
    )
    monkeypatch.setattr(sys, "argv", ["refresh", str(junit), "--output", str(output)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert output.read_text() == original


def test_duplicate_cases_from_multiple_runs_are_rejected(tmp_path):
    first = report(tmp_path, '<testcase classname="tests.test_a" name="test_a" time="1"/>')
    second = tmp_path / "second.xml"
    second.write_text(first.read_text())
    with pytest.raises(ValueError, match="duplicate"):
        report_weights([first, second], tmp_path, {"tests.test_a": "tests/test_a.py"})


def test_explicit_file_attribute_and_hidden_module_paths(tmp_path):
    source = tmp_path / ".github/ci/tests/test_ci.py"
    source.parent.mkdir(parents=True)
    source.touch()
    junit = report(
        tmp_path,
        '<testcase file=".github/ci/tests/test_ci.py" classname="tooling.Test" name="test_a" time="0.2"/>',
    )
    assert report_weights([junit], tmp_path, {}) == {".github/ci/tests/test_ci.py": 0.2}
    assert ".github.ci.tests.test_ci" in module_paths(Path(__file__).resolve().parents[3])
