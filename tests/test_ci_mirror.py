import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT, run_hook
from ci_mirror import mini_yaml
from ci_mirror.core import all_jobs, handle, task_runners

WORKFLOW = """
name: CI
on: [push]
env:
  GREETING: hello
  TOKEN: ${{ secrets.TOKEN }}
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: echo "$GREETING from $PART" > out.txt
        env:
          PART: test-job
      - name: Matrix only
        run: echo ${{ matrix.node }}
      - name: Check
        working-directory: sub
        run: |
          test -f ../out.txt
          echo ok > here.txt
  deploy:
    needs: test
    steps:
      - run: npm publish
"""

GITLAB = """
stages: [test]
variables:
  MODE: ci
.template:
  script: [echo hidden]
default:
  before_script:
    - echo before
unit:
  stage: test
  script:
    - echo unit-$MODE
lint:
  before_script: []
  script: echo lint
"""


@pytest.fixture
def repo(project):
    (project / ".github" / "workflows").mkdir(parents=True)
    (project / ".github" / "workflows" / "ci.yml").write_text(WORKFLOW)
    (project / ".gitlab-ci.yml").write_text(GITLAB)
    (project / ".pre-commit-config.yaml").write_text("repos:\n  - repo: x\n    hooks:\n      - id: ruff\n      - id: black\n")
    (project / "Makefile").write_text(".PHONY: test\nbuild:\n\tcc x\ntest: build\n\t./t\nVAR := 1\n")
    (project / "package.json").write_text(json.dumps({"scripts": {"dev": "vite", "lint": "eslint ."}}))
    (project / "sub").mkdir()
    return project


def test_mini_yaml_matches_expectations():
    doc = mini_yaml.load(WORKFLOW, force_mini=True)
    steps = doc["jobs"]["test"]["steps"]
    assert steps[1]["env"] == {"PART": "test-job"}
    assert steps[3]["run"] == "test -f ../out.txt\necho ok > here.txt\n"
    assert mini_yaml.load("a: 'it''s'\nb: \"x\\ty\"\nc: [1, two]\n", force_mini=True) == {"a": "it's", "b": "x\ty", "c": [1, "two"]}


def test_discovery(repo):
    refs = [j.ref for j in all_jobs(repo)]
    assert refs == ["github/ci.yml:test", "github/ci.yml:deploy", "gitlab:unit", "gitlab:lint", "pre-commit:all"]
    unit = next(j for j in all_jobs(repo) if j.name == "unit")
    assert [s.run for s in unit.steps] == ["echo before", "echo unit-$MODE"] and unit.env == {"MODE": "ci"}
    lint = next(j for j in all_jobs(repo) if j.name == "lint")
    assert [s.run for s in lint.steps] == ["echo lint"]
    tr = task_runners(repo)
    assert tr["make"][0] == "test" and "VAR" not in tr["make"] and tr["package.json scripts"][0] == "lint"


def test_session_summary(repo):
    text = handle({"hook_event_name": "SessionStart", "cwd": str(repo)})["hookSpecificOutput"]["additionalContext"]
    assert "github/ci.yml:test runs" in text and "`npm publish`" in text and "make: test, build" in text


def test_run_job_skips_actions_expressions_and_risky(repo, isolated):
    env = dict(os.environ, HOME=str(isolated))
    cli = [sys.executable, str(ROOT / "bin" / "ci-mirror")]
    r = subprocess.run(cli + ["run", "test", "--project", str(repo)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (repo / "out.txt").read_text().strip() == "hello from test-job"   # workflow + step env
    assert (repo / "sub" / "here.txt").exists()                              # working-directory honoured
    assert "skipped, uses actions/checkout@v4" in r.stdout and "only exist in CI" in r.stdout
    r = subprocess.run(cli + ["run", "deploy", "--project", str(repo)], capture_output=True, text=True, env=env)
    assert "deploys/publishes" in r.stdout
    r = subprocess.run(cli + ["run", "nope", "--project", str(repo)], capture_output=True, text=True, env=env)
    assert r.returncode == 2 and "no job" in r.stdout


def test_failing_step_stops(project, isolated):
    (project / ".gitlab-ci.yml").write_text("t:\n  script:\n    - exit 3\n    - touch never\n")
    r = subprocess.run([sys.executable, str(ROOT / "bin" / "ci-mirror"), "run", "t", "--project", str(project)],
                       capture_output=True, text=True, env=dict(os.environ, HOME=str(isolated)))
    assert r.returncode == 3 and not (project / "never").exists()


def test_empty_project_silent_and_script(project, isolated):
    assert handle({"hook_event_name": "SessionStart", "cwd": str(project)}) is None
    assert run_hook({"hook_event_name": "SessionStart", "cwd": str(project)}, isolated) == {}
