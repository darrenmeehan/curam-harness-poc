# curam — install & quickstart

## Requirements

- **Python 3.12+** (stdlib only — no third-party deps)
- An OpenAI-compatible endpoint with tool-calling (function calls):
  - [OpenRouter](https://openrouter.ai) — cloud, needs an API key (seen via
    `OPENROUTER_API_KEY`, or reuse pi's `~/.pi/agent/auth.json` automatically)
  - [Ollama](https://ollama.com) — local, free (set `default_provider = "ollama"`)
  - LiteLLM / vLLM / llama.cpp — any `/v1/chat/completions` server

## Install

### A. uv (recommended — installs a real `curam` binary, works from any dir)

```bash
cd ~/code/curam/curam-harness
uv tool install .
# -> Installed executable: curam
```

Now `curam` is on your PATH and usable from any directory. The **current
directory is always the workdir** — no config needed:

```bash
cd /some/project
curam "Add a --dry-run flag and a test for it"
```

### B. Run in place (dev checkout — needs the eval corpus)

```bash
cd ~/code/curam/curam-harness
python3 curam.py            # or: python3 ui.py
```

Lets you run the offline evals (`curam eval`, `curam tasks`) which are NOT
shipped with the installed wheel — they live in the repo checkout only.

### Reinstall after changes (dev loop)

```bash
cd curam-harness
uv build && uv tool install dist/*.whl --force
```



### A. Run in place (zero install)

```bash
git clone <this-repo-url> ~/code/curam/curam-harness
cd ~/code/curam/curam-harness
python3 curam.py            # or: python3 ui.py
```

No pip install needed. Add a shell alias:

```bash
alias curam="python3 ~/code/curam/curam-harness/curam.py"
```

### B. Symlink a bin

```bash
ln -s $(pwd)/curam.py ~/.local/bin/curam   # now `curam` from anywhere
```

(curam resolves its config/tools relative to its own file, so the repo can
live anywhere and the symlink still finds them.)

## Configure once

Edit `curam.toml`:

```toml
default_provider = "openrouter"      # or "ollama"
# [providers.openrouter] api_key_env = "OPENROUTER_API_KEY"
# budget.cap_usd = 0.20  (per-session ceiling)
```

The OpenRouter key is read from `OPENROUTER_API_KEY` env var, OR from pi's
`~/.pi/agent/auth.json` automatically (no extra setup if you use pi).

## First run

```bash
curam "Say hello"                      # smoke: greets without tools
curam "create README.md describing this repo"
curam --sli                            # what the session cost, tool stats
curam report                           # full evaluation report
```

## Build real software with it

The harness shines on real code, but be deliberate:

1. **Point it at a real scratch clone** — never your only copy of a repo:
   ```bash
   cd /tmp && git clone <your-repo> && cd <your-repo>
   curam "Add a --dry-run flag to the deploy script and a test for it"
   ```
2. **Give it a bounded, verifiable task** — something with tests or a build
   you can run to check its work. "Make the tests pass" beats "improve the
   architecture".
3. **Watch the realtime view**: you see thinking (dim), tool calls (`▶`),
   and results as they stream. Use `/verbose` and `/quiet` to toggle.
4. **When it asks** — a yellow `[curam:ask]` prompt means a design fork,
   ambiguity, or irreversible op. That's the sandbox working, not a failure.
5. **Reject destructive auto-run**: policy already denies `rm -rf /`,
   `--no-preserve-root`, `curl|sh`, decode-and-execute. Add more in
   `policy.py` if the eval shows gaps.

## Verify your install

```bash
# offline self-checks (no network, no model)
python3 eval_corpus.py       # 8 offline scenarios → passed 8/8
curam tasks                  # lists the 10 eval tasks

# live (needs a provider)
curam "Create a file named hi.py that prints hi and run it"
curam report                 # shows cost, tools, per-session stats
```

## Observability

Every run is journaled to `~/.local/share/curam/curam.db` (SQLite WAL):

- `curam --sli` — quick session summary (events, cost, tokens, skills)
- `curam report` — evaluation view: per-session tool calls, asks, cost,
  skills used, errors, denied
- `curam report --since 2h --json` — machine-readable for your own dashboards
- `curam eval` — re-run the offline corpus to see the harness still holds

That journal is the audit trail: everything the agent did, in order, with
cost. It's how you judge whether it's doing real work or spinning.

## Security notes (read before letting it touch real code)

- **No sandbox yet**: shell runs as you in the workdir. Use scratch clones.
- **Policy is allow-most by default**; destructive patterns are deny/ask.
- **Prompt injection is real** (the eval proved it): don't point it at an
  untrusted repo with secrets without a firewall (docs/curam.md — deferred).
- PII scrubber is on by default; the journal keeps the raw result (audit).
