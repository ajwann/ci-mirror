"""Discover what CI and the project's task runners run, summarize it, and run it locally."""
from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

import hookio

from . import mini_yaml

PLUGIN = "ci-mirror"

DEFAULTS = {
    "session_summary": True,
    "max_summary_chars": 3000,
    "risky_patterns": [r"\bdeploy", r"\bpublish\b", r"\brelease\b", r"docker\s+push", r"\bgh\s+release",
                       r"\btwine\s+upload", r"\bkubectl\s+(apply|delete|rollout)", r"\bterraform\s+(apply|destroy)",
                       r"\baws\s+", r"\bgcloud\s+", r"\bnpm\s+publish", r"\bcargo\s+publish", r"\bvercel\b",
                       r"\bfly\s+deploy", r"\bgit\s+push"],
}

GITLAB_RESERVED = {"stages", "variables", "default", "include", "workflow", "image", "services", "cache",
                   "before_script", "after_script", "pages"}
INTERESTING_TARGETS = ("ci", "check", "test", "tests", "lint", "fmt", "format", "typecheck", "type-check",
                       "build", "verify", "all", "validate", "e2e")


class Step:
    def __init__(self, name: str, run: Optional[str] = None, uses: Optional[str] = None,
                 workdir: Optional[str] = None, env: Optional[dict] = None, shell: Optional[str] = None):
        self.name, self.run, self.uses, self.workdir, self.env, self.shell = name, run, uses, workdir, env or {}, shell

    def to_dict(self) -> dict:
        return {k: v for k, v in vars(self).items() if v}


class Job:
    def __init__(self, source: str, name: str, steps: list[Step], env: Optional[dict] = None):
        self.source, self.name, self.steps, self.env = source, name, steps, env or {}

    @property
    def ref(self) -> str:
        return f"{self.source}:{self.name}"


def _plain_env(d) -> dict:
    """Only literal env values; anything with ${{ }} is not reproducible locally."""
    if not isinstance(d, dict):
        return {}
    return {str(k): str(v) for k, v in d.items() if v is not None and "${{" not in str(v)}


def _load_yaml(path: Path):
    try:
        return mini_yaml.load(path.read_text(errors="replace"))
    except Exception as e:  # noqa: BLE001
        hookio.log_error(PLUGIN, f"yaml {path}: {e}")
        return None


# ---------------------------------------------------------------- discovery

def github_jobs(project: Path) -> list[Job]:
    jobs = []
    wf_dir = project / ".github" / "workflows"
    if not wf_dir.is_dir():
        return jobs
    for wf in sorted(list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml"))):
        doc = _load_yaml(wf)
        if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
            continue
        wf_env = _plain_env(doc.get("env"))
        defaults = ((doc.get("defaults") or {}).get("run") or {}) if isinstance(doc.get("defaults"), dict) else {}
        for jid, job in doc["jobs"].items():
            if not isinstance(job, dict):
                continue
            jdef = ((job.get("defaults") or {}).get("run") or {}) if isinstance(job.get("defaults"), dict) else {}
            steps = []
            for i, st in enumerate(job.get("steps") or []):
                if not isinstance(st, dict):
                    continue
                steps.append(Step(
                    name=str(st.get("name") or (st.get("run") or st.get("uses") or f"step {i + 1}")).split("\n")[0][:80],
                    run=st.get("run") if isinstance(st.get("run"), str) else None,
                    uses=st.get("uses") if isinstance(st.get("uses"), str) else None,
                    workdir=st.get("working-directory") or jdef.get("working-directory") or defaults.get("working-directory"),
                    env=_plain_env(st.get("env")),
                    shell=st.get("shell") or jdef.get("shell") or defaults.get("shell")))
            if job.get("uses"):  # reusable workflow call
                steps.append(Step(name=f"reusable workflow {job['uses']}", uses=str(job["uses"])))
            jobs.append(Job(f"github/{wf.name}", str(jid), steps, {**wf_env, **_plain_env(job.get("env"))}))
    return jobs


def gitlab_jobs(project: Path) -> list[Job]:
    path = project / ".gitlab-ci.yml"
    doc = _load_yaml(path) if path.is_file() else None
    if not isinstance(doc, dict):
        return []
    default = doc.get("default") if isinstance(doc.get("default"), dict) else {}
    glob_before = doc.get("before_script", default.get("before_script"))
    variables = _plain_env(doc.get("variables"))
    jobs = []
    for name, job in doc.items():
        if name in GITLAB_RESERVED or str(name).startswith(".") or not isinstance(job, dict):
            continue
        script = job.get("script")
        if script is None:
            continue
        lines = []
        for part in (job.get("before_script", glob_before), script):
            if isinstance(part, str):
                lines.append(part)
            elif isinstance(part, list):
                lines += [str(x) for x in part if x is not None]
        steps = [Step(name=l.split("\n")[0][:80], run=l) for l in lines]
        jobs.append(Job("gitlab", str(name), steps, {**variables, **_plain_env(job.get("variables"))}))
    return jobs


def precommit_job(project: Path) -> list[Job]:
    path = project / ".pre-commit-config.yaml"
    doc = _load_yaml(path) if path.is_file() else None
    if not isinstance(doc, dict):
        return []
    ids = [h.get("id") for r in doc.get("repos") or [] if isinstance(r, dict)
           for h in r.get("hooks") or [] if isinstance(h, dict) and h.get("id")]
    return [Job("pre-commit", "all", [Step(name=f"pre-commit hooks: {', '.join(ids[:12])}",
                                           run="pre-commit run --all-files")])]


def task_runners(project: Path) -> dict[str, list[str]]:
    """{'make': [targets], 'just': [...], 'npm scripts': [...], 'tox': [...], 'nox': [...]} (interesting ones first)."""
    out: dict[str, list[str]] = {}
    mk = next((project / n for n in ("Makefile", "makefile", "GNUmakefile") if (project / n).is_file()), None)
    if mk:
        targets = re.findall(r"^([A-Za-z0-9][\w.-]*)\s*:(?!=)", mk.read_text(errors="replace"), re.M)
        out["make"] = _rank(dict.fromkeys(targets))
    jf = next((project / n for n in ("justfile", "Justfile", ".justfile") if (project / n).is_file()), None)
    if jf:
        recipes = re.findall(r"^@?([A-Za-z_][\w-]*)(?:\s+[^:=\n]*)?:(?!=)", jf.read_text(errors="replace"), re.M)
        out["just"] = _rank(dict.fromkeys(recipes))
    pkg = project / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts") or {}
            if isinstance(scripts, dict):
                out["package.json scripts"] = _rank(scripts)
        except (ValueError, AttributeError):
            pass
    tox = project / "tox.ini"
    if tox.is_file():
        cp = configparser.ConfigParser(interpolation=None)
        try:
            cp.read_string(tox.read_text(errors="replace"))
            envlist = cp.get("tox", "envlist", fallback="")
            out["tox"] = [e.strip() for e in re.split(r"[,\n]", envlist) if e.strip()]
        except configparser.Error:
            pass
    nox = project / "noxfile.py"
    if nox.is_file():
        out["nox"] = re.findall(r"@nox\.session[^\n]*\n(?:@[^\n]*\n)*def\s+(\w+)", nox.read_text(errors="replace"))
    return {k: v for k, v in out.items() if v}


def _rank(names) -> list[str]:
    names = [n for n in names if not n.startswith(".")]
    def rank(n: str) -> int:
        base = n.split(":")[0]
        return INTERESTING_TARGETS.index(base) if base in INTERESTING_TARGETS else len(INTERESTING_TARGETS)
    return sorted(names, key=rank)


def all_jobs(project: Path) -> list[Job]:
    return github_jobs(project) + gitlab_jobs(project) + precommit_job(project)


# ---------------------------------------------------------------- summary

def summary(project: Path, cfg: dict) -> Optional[str]:
    jobs = all_jobs(project)
    runners = task_runners(project)
    if not jobs and not runners:
        return None
    parts = []
    for job in jobs:
        runs = [s.run.strip().replace("\n", "; ") for s in job.steps if s.run]
        if not runs:
            continue
        shown = "; ".join(f"`{r[:120]}`" for r in runs[:6]) + (" …" if len(runs) > 6 else "")
        parts.append(f"{job.ref} runs {shown}.")
    for name, items in runners.items():
        parts.append(f"{name}: {', '.join(items[:10])}{' …' if len(items) > 10 else ''}.")
    text = ("ci-mirror: what CI checks in this repo. " + " ".join(parts) +
            " `ci-mirror list` shows every job; `ci-mirror run <job>` runs a job's steps locally.")
    return hookio.clip(text, int(cfg["max_summary_chars"]))


# ---------------------------------------------------------------- local run

def find_job(jobs: list[Job], query: str) -> list[Job]:
    exact = [j for j in jobs if j.ref == query or j.name == query]
    return exact or [j for j in jobs if query in j.ref]


def plan(job: Job, cfg: dict, include_risky: bool) -> list[tuple[Step, Optional[str]]]:
    """[(step, skip_reason or None)]"""
    out = []
    risky = [re.compile(p, re.I) for p in cfg["risky_patterns"]]
    for st in job.steps:
        if not st.run:
            out.append((st, f"uses {st.uses} (an action; set up the equivalent locally)" if st.uses else "no run"))
        elif "${{" in st.run:
            out.append((st, "uses ${{ }} expressions that only exist in CI"))
        elif not include_risky and any(r.search(st.run) for r in risky):
            out.append((st, "looks like it deploys/publishes (use --include-risky to run it)"))
        else:
            out.append((st, None))
    return out


def run_job(project: Path, job: Job, cfg: dict, include_risky: bool = False, dry_run: bool = False,
            keep_going: bool = False, printer=print) -> int:
    worst = 0
    for st, skip in plan(job, cfg, include_risky):
        if skip:
            printer(f"⏭  {st.name}: skipped, {skip}")
            continue
        cwd = project / (st.workdir or ".")
        printer(f"▶  {st.name}" + (f"  (in {st.workdir})" if st.workdir else ""))
        if dry_run:
            printer("   " + st.run.strip().replace("\n", "\n   "))
            continue
        shell = ["bash", "-eo", "pipefail", "-c"] if (st.shell or "bash").startswith("bash") else ["sh", "-ec"]
        env = dict(os.environ, CI="true", **job.env, **st.env)
        r = subprocess.run(shell + [st.run], cwd=cwd, env=env)
        printer(f"   exit {r.returncode}")
        if r.returncode:
            worst = r.returncode
            if not keep_going:
                printer(f"✗  stopped at '{st.name}' (use --keep-going to continue)")
                return worst
    return worst


def handle(event: dict) -> Optional[dict]:
    if event.get("hook_event_name") != "SessionStart":
        return None
    project = hookio.project_dir(event)
    cfg = hookio.load_config(PLUGIN, project, DEFAULTS)
    if not cfg.get("session_summary", True):
        return None
    text = summary(project, cfg)
    return hookio.context("SessionStart", text) if text else None
