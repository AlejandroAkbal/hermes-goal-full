# Contributing

Thanks for taking the time to look at this. The plugin is small, and the rules are short.

## Before you start

- For a bug, open an issue with the Hermes version (`hermes --version`), the surface you used (classic
  CLI, Desktop, TUI, gateway), and what you expected instead.
- For a change in behaviour, open an issue first. This plugin deliberately writes through Hermes'
  goal engine; a change that forks that behaviour is unlikely to be merged.
- For a security issue, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Setting up

goal-full needs a Hermes checkout to import. You do not need to install it into your global Python if
you put it on `PYTHONPATH`.

```bash
git clone https://github.com/AlejandroAkbal/hermes-goal-full.git
cd hermes-goal-full
git clone --depth 1 --branch v0.23.3 https://github.com/NousResearch/hermes-agent.git .hermes-src
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ./.hermes-src
pip install pytest
```

If you already have Hermes installed, point at it instead:

```bash
PYTHONPATH=/path/to/hermes-agent pytest tests/ -q
```

## Running the tests

```bash
pytest tests/ -q
```

The suite creates a temporary `HERMES_HOME`, replaces model calls with fixed replies or injected
callables, and never touches your real session database. It needs no network and no running gateway.

Expected today: 55 passed.

## What a good pull request looks like

- One change, one reason. Do not mix a refactor with a behaviour change.
- A test for anything that changes behaviour. The existing tests exercise the command handler, the
  tool, the removal judge, and both hooks; extend them rather than adding a parallel suite.
- No new dependency unless it is unavoidable. The plugin has no third-party imports.
- Keep private imports in one place. If you touch `hermes_cli.goals` or `agent.auxiliary_client`,
  say which Hermes version you tested against.
- Run the plugin validator:

```bash
hermes plugins validate .
hermes plugins doctor . --ci
```

- Keep the README honest. If a claim no longer matches the code, fix one of them in the same pull
  request.

## Style

- Python 3.11 or newer, standard library only.
- No comments that restate the code. A comment should explain a decision or a constraint.
- Prefer a small function with a clear name over a clever one.
- The existing code uses `from __future__ import annotations`, typed signatures, and module-level
  constants. Follow that.

## Commit messages

A short imperative subject line, then a body explaining why when the reason is not obvious. "Fix gate
timeout clamp" is fine. "Update code" is not.

## License

By contributing, you agree that your contribution is licensed under the MIT license in
[LICENSE](LICENSE).
