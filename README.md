# ci-mirror

Tells Claude what CI will check, and runs the same steps locally. At session start it reads:
- `.github/workflows/*.yml`, with job steps, `working-directory`, `env` and `defaults.run`
- `.gitlab-ci.yml`, with `script`, `before_script`, `variables`, hidden `.templates` and `default`
- `.pre-commit-config.yaml`
- Makefile and justfile targets, package.json scripts, tox envs and nox sessions

From these it summarizes the commands CI runs, so Claude validates with the same commands before
saying it's done.

```
ci-mirror list                 # every job and its steps, plus task runners
ci-mirror run test             # run job `test` locally (bash -eo pipefail, CI=true, literal env applied)
ci-mirror run github/ci.yml:lint --dry-run
```

Steps that `uses:` an action or contain `${{ }}` expressions are skipped with a reason. So is anything
that looks like a deploy or publish, unless you pass `--include-risky`. The YAML reader is a small
stdlib parser. PyYAML is used when it's installed.

**Hook:** `SessionStart`. **Config:** `{"session_summary": true, "max_summary_chars": 3000, "risky_patterns": [...]}`.

## Install

From Anthropic's plugin directory, once listed: search for `ci-mirror` in `/plugin directory` in Claude Code, or under **Customize > Plugins** on claude.ai.

To try it from a clone for one session: `claude --plugin-dir /path/to/ci-mirror`.

This plugin is for Claude Code: it is built on hooks and a command-line tool in `bin/`, which claude.ai chat does not load.

## Configuration

Settings are read from `~/.claude/ci-mirror.json` and then `<project>/.claude/ci-mirror.json`. The project file wins, objects merge by key, and a broken file is skipped and logged.

## What it runs and stores

It is Python 3.9+ standard library only, makes no network requests, and sends nothing anywhere. Every hook fails open: an internal error is logged to `~/.claude/ci-mirror/errors.log` and the hook exits 0.

- Reads CI and task-runner config files in the project. It writes nothing except the error log.
- `ci-mirror run` executes the selected CI steps locally with `bash -eo pipefail`, only when you or Claude run it.

## Development

```
python3 -m pytest -q
claude plugin validate --strict .
```

`docs/HOOK_SCHEMA.md` records the hook payloads this plugin depends on.

## License

MIT. See `LICENSE`.
