# curam — Rust Harness: Implementation Plan

**Status:** approved — defaults taken (Decisions §13: 1a, 2a, 3a, 4a, 5a, 6a). Implementation in progress: H0 (curam-core split), H1 (CLI + config + init + doctor), H2 (provider/SSE + budget + journal + sli), H3 (agent loop + file tools + policy + sanitize), and H4 (caged shell + typed run + ask layer + fail-closed exec) landed.

**Inputs:** Python POC (`curam-harness/`, frozen), Rust security core (`curam-security/`, e01 complete), design docs (`docs/curam.md`, `docs/architecture.md`, `docs/rust-security-core.md`, `docs/rust-libraries.md`, `docs/curam-slos.md`).
**Goal:** a native Rust `curam` binary — the harness itself — built semantic-for-semantic on the proven POC, linking the security core as a library and the cage over the existing ndjson contract, with the UX the POC explicitly deferred to the Rust phase (real TUI, streaming, typed exec).

---

## 0. TL;DR — scope

| In | Out (this plan) |
| --- | --- |
| Port POC: loop, 6 tools, config, journal, budget, skills, asks, evals, report | Harness self-sandbox (bwrap profile per project) |
| Wire curam-security: policy/sanitize/hosts/vuln as a library, `curam-run` as the only exec path | pi-extensions shim |
| New Rust-phase UX: SSE streaming, ratatui TUI, typed `run` tool, `init` + `doctor` + config validation | Phone bridge / daemon (curamd — future crate) |
| Preserve every POC config key so existing `curam.toml` files still parse | OTel/Arize telemetry export, evlog readers |

Repo decision + crate list are review points (§2, §4). The one prerequisite that touches the security repo is a small refactor (§2) — nothing in `curamsec`'s public API moves.

---

## 1. What we port (inventory)

Python POC module → Rust home → strategy. "1:1" means behavior-preserving port with parity tests (same pattern as `policy.py` → `rules.rs`).

| Python POC | Role | Rust home | Strategy |
| --- | --- | --- | --- |
| `curam.toml` | config | `config.rs` | 1:1 keys + new sections (§5) |
| `ui.py` | CLI / REPL | `cli.rs` + `tui.rs` | one-shot CLI + full ratatui TUI + legacy REPL mode |
| `agent.py` | the loop | `agent.rs` | port; add streaming, cancel, typed `run` |
| `tools.py` | read / write / edit / shell | `tools.rs` | shell → caged (never raw) + new typed `run` tool |
| `policy.py` | stage-1 gate | **curam-core** `rules.rs` (already ported) | delegate, add per-project rule overlay |
| `classify.py` | stage-2 judge | `classifier.rs` | 1:1 (same provider seam, model_override) |
| `sanitize.py` | PII scrub | `sanitize.rs` | 1:1, `regex` crate; keep RAW-in-journal rule |
| `ask.py` | judgment/approval/stuck | `ask.rs` | 1:1 logic; prompts become TUI modals |
| `journal.py` | SQLite WAL journal | `journal.rs` (rusqlite) | **schema-identical** (freeze in `proto/`) |
| `budget.py` | cost accounting | `budget.rs` | 1:1 (usage fields → USD, `cost_unknown` event) |
| `provider.py` | OpenAI-compatible client | `provider.rs` (ureq) | rewrite: + SSE streaming, same request shape |
| `cage.py` | ndjson client | `cage.rs` | rewrite client over the same `cage/1` contract |
| `skills.py` | progressive disclosure | `skills.rs` | 1:1 + startup lint |
| `report.py` / `ui.sli_summary` | o11y | `report.rs` | 1:1 SQL surface → `curam sli` / `curam report` |
| `eval_corpus.py` / `run_eval.py` | eval runner | `eval.rs` | port runner; **corpus stays as data** (10 tasks, unchanged) |

---

## 2. Prerequisite: split `curamsec` into `curam-core` + pyo3 shim

**Why.** `curamsec` is a PyO3 `cdylib`. A Rust harness cannot link it cleanly (pyo3 `extension-module` mode + the Python runtime). The valuable logic — `rules.rs`, `shlex.rs`, `host.rs`, `lists.rs`, `osv.rs` — is already pure Rust. The split is mechanical and the tests already exist.

**Work (in `curam-security`, new Cargo workspace):**

```
curam-security/
  Cargo.toml            # [workspace] members: crates/*, bin/curam-run
  crates/curam-core/    # pure Rust lib (NO pyo3, NO python):
                        #   rules, shlex, host, lists, osv + Policy facade:
                        #   classify(cmd, allow_rules) -> Verdict
                        #   scan_context(text, threshold) -> Vec<Finding>
                        #   match_host / check_pkg / lists update + verify
  crates/curamsec/      # pyo3 shim: thin wrapper over curam-core.
                        #   keeps the abi3 wheel surface byte-identical:
                        #   classify/scan_context/match_host/check_pkg/
                        #   reload_host_lists/lists_update + Verdict/Finding/
                        #   HostMatcherPy/Advisory classes
  bin/curam-run/        # cage binary, behavior unchanged; consumes curam-core
                        #   for verdict chains where cheap
```

**Gate — nothing above may change behavior:**

- `cargo test` (curam-core unit + parity fixture) green as today
- `pytest tests/` in curam-security green (T1–T7 incl. `test_parity.py`, `cage_integration.py`, `contract_replay.py`)
- `harness-wiring/check_wiring.py` green (Python harness still works unbroken)

**Consumption.** The harness depends on `curam-core` via path dep (`../curam-security/crates/curam-core`) — decision point §13. `curam-run` stays an external binary found via `CURAM_RUN_BIN` / `[cage] binary` / `PATH` (the Python harness already uses this pattern, keep it).

---

## 3. Repo layout (decision point)

**Recommended:** new standalone repo `~/code/curam-harness-rs`.

```text
curam-harness-rs/
  Cargo.toml            # single binary crate "curam" (+ lib target for tests)
  src/
    main.rs             # clap wiring, exit codes, signals
    config.rs           # TOML load/merge/validate, defaults, `init` templates
    provider.rs         # ureq OpenAI-client, SSE stream, usage capture
    agent.rs            # loop: messages -> provider -> tools -> repeat
    tools.rs            # read/write/edit/run/shell + JSON schemas
    classifier.rs       # stage-2 judge (benign/injection/credential/destructive)
    sanitize.rs         # deterministic PII scrub (port of sanitize.py)
    ask.rs              # approval / judgment / stuck (TUI modal seam)
    journal.rs          # rusqlite WAL, events schema frozen in proto/
    budget.rs           # cost accounting from usage
    skills.rs           # frontmatter md skills, progressive disclosure, lint
    cage.rs             # curam-run ndjson client (cage/1)
    tui.rs              # ratatui: conversation, status rail, modals, block view
    repl.rs             # plain REPL fallback (non-tty / --no-tui)
    report.rs           # sli + report commands (one-query-per-SLI)
    eval.rs             # eval runner over eval_tasks/ corpus
  proto/                # journal schema + ndjson contract + event types (moved/copied, versioned)
  eval_tasks/           # vendored from curam-harness (data, unchanged)
  tests/                # integration + parity fixtures
```

Same logic as the security-core decision: open-source-first, own issues/CI/releases, the POC repo stays frozen data. Alternative (keep as `rust/` inside `curam-harness`) is rejected for the same reasons the security core was split out — say the word if you'd rather one repo.

---

## 4. Crates (finalized; follows `rust-libraries.md`, verified current)

| Concern | Crate | Why / notes |
| --- | --- | --- |
| CLI | **clap** 4.x | already decided in docs; derive-style, small, fast; subcommands + suggestions |
| TUI | **ratatui** 0.30.x (pin exact) | decided in docs; active (0.30.2, 2026-06); crossterm backend, works in plain terminals |
| Config | **toml** (parse-only) | stdlib-free; we don't need to *write* TOML except `init` templates (strings) |
| HTTP | **ureq** | blocking IO (no tokio), streaming via `into_body().into_reader()` → clean SSE loop in a worker thread; tiny dep tree |
| SQLite | **rusqlite** 0.40.x, `bundled` | WAL journal 1:1 port; `check_same_thread=False` equivalent via `Connection` in the agent thread |
| Matching/ha | **regex**, **aho-corasick**, **serde/serde_json**, **sha2** | reused from `curam-core` for sanitize/policy/vuln |
| Async | **none** | still no server-shaped artifact; streaming = blocking reader thread + channel to TUI. curamd (AF_UNIX/JSON-RPC, tokio+axum) stays a future separate crate |

Deliberately not added: pyo3 (harness is native), tokio/axum, any DNS crate, tui-rs (unmaintained), suggest crate, any keyring crate in v1 (keep env + pi auth.json fallback parity; OS keychain listed as later option in §13).

---

## 5. Config — `curam.toml` spec

**Layering (later wins):** compile-time defaults → `~/.config/curam/curam.toml` (user) → `./curam.toml` (project) → explicit `--config FILE` → CLI flags/env. Merge is per-table shallow overlay. Unknown keys = warning to stderr (never fatal). Wrong type / invalid enum = hard error naming the exact key path (`config: [budget] cap_usd: expected number, got "abc"`).

**Env overrides:** everything the POC honored plus: `CURAM_CONFIG`, `CURAM_RUN_BIN`, `CURAM_LISTS_DIR`, `CURAM_NO_COLOR`, existing `*_API_KEY` via `api_key_env` (with the pi `~/.pi/agent/auth.json` fallback preserved).

```toml
# curam.toml — full reference (every key below is valid; defaults shown)
default_provider = "openrouter"          # or "ollama"

[providers.ollama]                       # any OpenAI-compatible endpoint
base_url = "http://localhost:11434/v1"
model = "qwen3-coder"
api_key_env = ""                         # empty = no auth
context_window = 32768
reasoning_effort_supported = false
timeout_s = 120                          # NEW (rust): connect+read timeout
max_tokens = 8192                        # NEW (rust): completion cap (optional; protects budget)
stream = true                            # NEW (rust): SSE streaming, default on

[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"
model = "deepseek/deepseek-v4-flash"
api_key_env = "OPENROUTER_API_KEY"
context_window = 1000000
reasoning_effort = "high"                # pi's defaultThinkingLevel parity

[budget]
cap_usd = 0.20                            # 0 = uncapped
[budget.price_per_mtok]                   # USD per 1M tokens; unknown = free
"deepseek/deepseek-v4-flash" = 0.10

[pii]
level = "deterministic"                   # off | deterministic (local_llm deferred)
placeholder = "{REDACTED}"
# internal_domains = ["thinkcentre.tailnet"]

[classifier]                              # stage-2 judge; off by default
enabled = false
model = ""                                # model_override on main provider

[ui]                                      # UX behaviour
theme = "auto"                            # auto | dark | light | plain
show_thinking = true                      # echoed interim text (TUI: dim stream)
show_commands = true                      # tool calls + results
max_output_chars = 8000                   # tool result trim (was MAX_OUTPUT_CHARS)
time_format = "relative"                  # relative | clock

[tools]                                   # NEW (rust) — the exec surface policy
shell = true                              # free-text shell tool (caged); false = off
run = true                                # NEW typed exec tool (argv array) — v1 default path
max_read_chars = 60000
timeout_default_s = 60

[run]                                     # NEW — typed exec defaults (the V1 design target)
default_net = true                        # egress in cage (git/pip/cargo need it)
default_timeout_s = 120
allow_programs = []                       # local overlay, feeds curam-core classify()
ask_programs = ["git", "curl", "wget", "scp", "bash", "sh"]
deny_programs = []

[cage]
binary = "curam-run"                      # name on PATH, or absolute path
lists_dir = "~/.local/share/curam/lists"  # feeds match_host + geo
geo = ""                                  # ISO-3166 code to block in the cage netns
fallback = "deny"                         # curam-run missing: deny | ask | raw (raw warns loudly)
                                          #   POC silently degraded to raw — NOT ported; loud default deny

[agent]
system_prompt = ""                        # override replaces built-in default
skills_dir = "skills"                     # relative to workdir
skills_max_description_chars = 240        # lint warnings (operator-facing)
skills_max_body_lines = 200
max_iterations = 25
stuck_threshold = 3

[policy.rules]                            # per-project overlay; core tables stay in curam-core
allow = []
ask = ["rm", "systemctl", "sudo"]         # example from POC
deny = []

[journal]
path = "~/.local/share/curam/curam.db"    # SQLite WAL, POC schema (frozen in proto/)
# evlog = "~/.local/share/curam/sessions/" # NOT in v1 (see §13; SQLite is truth, JSONL export stays)
keep = "90d"                              # NEW: report pruning scope (report window filter)

[workdir]
path = "./work"
```

Back-compat: every POC key parses to the same meaning. New keys are additive. `curam config --validate` will print per-key provenance (file or default).

---

## 6. CLI surface (clap)

```
curam [TASK...]              one-shot task (non-interactive mode if not a tty)
curam repl                   plain REPL fallback (--no-tui)
curam tui                    full-screen TUI (DEFAULT when tty + term supports it)
curam init [DIR]             scaffold curam.toml + workdir + skills/ + evals/ + .gitignore
curam config [--show] [--validate] [--path] [--set KEY=VALUE]
curam doctor                 preflight: provider reachable, curam-run present,
                             lists fresh, terminal capabilities, config sane
curam sli                    journal summary (the SLO SQL surface)
curam report [--since 2h|30m|1d] [--json]
curam evals [--task NAME] [--json] [--no-network]
curam tasks                  list eval tasks from the vendored corpus
curam lists (update|status|pin)   delegate to curam-core lists tooling
```

Global flags: `-C/--config FILE`, `--db PATH`, `-v/--verbose`, `-q/--quiet`, `--no-stream`, `--no-tui` (plain output), `--color auto|always|never`, `--json` (select outputs), `--provider NAME`, `--model ID`, `--version`, `-h/--help`.

**Exit codes:** `0` ok · `1` runtime error · `2` usage/config error · `3` provider unreachable · `4` budget halt · `5` denied by policy/human · `130` SIGINT. Every nonzero path prints a human sentence + the fix (POC rule: failures are never silent).

---

## 7. UX spec — the "polished" part (POC-scoped Rust items)

### 7.1 Streaming is the backbone ("never looks hung" becomes structural)

- Provider calls stream by default (SSE `data:` lines) through a worker thread → mpsc channel → TUI / REPL.
- Thinking renders as it arrives (dim); tool calls appear as headers with elapsed time; results foldable.
- Non-tty one-shot keeps the POC spinner + elapsed seconds; `--no-stream` forces request/response with spinner.
- Read timeout + keepalive safety: if no SSE byte for `timeout_s`, hard error with the same "provider unreachable" loudness as POC.

### 7.2 ratatui TUI (the deferred TUI, now built)

- **Main pane** — full conversation, native semantics: colours per event kind (POC palette), thinking as collapsible dim block, tool calls as `▶ name {...}` foldable to result summary.
- **Status rail** (right, fixed width) — provider/model · reasoning level · ctx fill % · `$ spent / cap` · iterations · last tool latency · cage status (`caged·net-on` / `caged·net-off` / `RAW!`).
- **Bottom input** — prompt line, `/`-commands with completion: `/help /verbose /quiet /sli /jsonl PATH /report /config /exit`.
- **Modal layer** for asks:
  - *Approval* (policy ask/deny): command + reason + `y / n / a` (allow once / deny / allow for session) — session-grant is journaled, counts to the fatigue SLI.
  - *Judgment* (`ask_human`): numbered options + free text, exactly POC semantics.
  - *Stuck* nudge: 3 options, POC wording.
- **Block-debug view** (`Ctrl-b`): last ~20 tool calls + live output stream — "what is this model doing" without leaving the TUI.
- **Cancel**: `Ctrl-c` cancels the in-flight model wait/tool (graceful: journal `error` event, return to input); second `Ctrl-c` in 1s quits. POC gap fixed.
- **Keys:** `Enter` submit · `Esc` cancel current ask · `Ctrl-b` block view · `Ctrl-c` cancel/quit · `PgUp/PgDn` scroll.

### 7.3 Non-interactive / scripting

- Piped stdin or `--quiet` → plain text, no ANSI, no spinner, no TUI; `--jsonl PATH` exports the journal after the run (scripts keep a machine-read audit).

### 7.4 Errors, `doctor`, init

- All POC loud-failure messages preserved and hardened (provider down shows `base_url` + fix; budget halt; iteration cap).
- `curam doctor` preflights and prints a one-screen health card with checkmarks and fixes.
- `curam init` scaffolds a valid config + directories so the first run is green.

---

## 8. Security integration (building on curam-security)

**Single exec path (invariant #1):** `tool_run` (`argv` array, policy-checked as a parsed array — the V1 target from `curam.md`) and the opt-in `tool_shell` (free-text, extra guard: `classify` on the joined command) **both** execute only through `curam-run` over the `cage/1` ndjson contract. No second exec path exists in the binary.

| POC/design property | Enforced in Rust by |
| --- | --- |
| Stage-1 deterministic gate | curam-core `classify` (policy parity fixture, extended) + `[policy.rules]` + `[run]` overlays |
| Exec-of-repo-file ⇒ ask (injection fix, e01s03) | curam-core `classify` `exec_repo_script` rule (unchanged) |
| No secrets cross the provider boundary | `sanitize.rs` scrub on every outbound message + classifier input; journal keeps RAW (`scenario_pii_scrub` eval ports to prove it) |
| Destructive ⇒ approval | ask layer + `approval_granted` journal event required; `auto_grant` eval-only flag ported |
| Network egress boundary | cage `net`/`geo`/`allow_hosts`/`deny_hosts` per tool call from config; verdicts journaled as `tool_verdicts` (provable chain `{action, rule_id, source, source_hash, evidence}`) |
| **Fail closed** (differs from POC) | cage unavailable + `[cage] fallback = "deny"` (default) → tool returns denied with the build/fix message; `raw` opt-out prints a red warning every call |
| Stage-2 judge (classify.py) | `classifier.rs`, same provider seam (`model_override`), degrades to benign on error — policy stays source of truth |
| Package vuln (check_pkg / OSV) | curam-core `check_pkg` exposed as an eval-phase tool later; not in v1 tool surface (§13) |

All verdicts keep the provable chain and get journaled — the SLO audit property is preserved end to end in Rust.

---

## 9. Build phases (each lands end-to-end with runnable checks)

| # | Builds | Gated by (tests) | Milestone |
| --- | --- | --- | --- |
| **H0** | (in curam-security) workspace split: `curam-core` + pyo3 shim + `bin/curam-run` | `cargo test` + `pytest tests/` T1–T7 + `check_wiring.py` all green, unchanged | core is linkable by a native binary |
| **H1** | repo skeleton, `config.rs` (toml + layering + validate), clap CLI, `init`, `doctor`, exit codes | golden/negative config tests; legacy POC `curam.toml` parses identically; `--help` snapshot | `curam init && curam doctor` green with zero LLM |
| **H2** | `provider.rs` (ureq, SSE, usage capture), `budget.rs`, `journal.rs` (rusqlite, POC schema) | fixture-replay provider tests (recorded response bodies incl. SSE chunks); usage→cost math (POC asserts); journal reopen/resume/crash + WAL pragmas | `curam sli` prints a real session |
| **H3** | `agent.rs` loop + `tools.rs` (read/write/edit) + policy via curam-core + `sanitize.rs` | extended parity fixture (policy + sanitize cases); tool self-checks; greeting guard; PII invariant test | agent completes a task through tools, journal has full chain |
| **H4** | `run` tool + caged `shell` + `cage.rs` client + `ask.rs` approval/judgment | cage integration (T5 replay through the Rust client: egress denied, localhost allowed, timeout, `/etc` read-only); approval flows with recorded answers; fail-closed test | destructive command cannot run without an approval row |
| **H5** | `tui.rs` + streaming render + repl.rs polish + block view + Ctrl-c cancel | TUI smoke tests (widget layout under 24×80), manual QA checklist; stream/fallback path tests | human-in-loop judgement flows feel instant |
| **H6** | `skills.rs` + `classifier.rs` + `report.rs` (sli/report) | skills lint + progressive disclosure tests; classifier degrade tests; sli SQL surface parity vs POC output | `curam sli`/`report` numbers match POC queries |
| **H7** | `eval.rs` runner over vendored 10-task corpus | corpus verify.sh gates (same contract: repo-state only); N1–N10 tallies | **go/no-go per SLOs §11** |

Milestone mapping to the old roadmap: H3 closes nothing new (POC already proven); H4 is the "typed run-only exec, cage, fail-closed" milestone; H5 is the deferred TUI + streaming; H7 is the SLO gate.

---

## 10. Testing & parity strategy

- **Parity fixtures are data, not code** (the curam-core pattern): extend `parity_fixture.json` with sanitize cases; add a recorded provider-response fixture set (no live model in CI); freeze journal schema + event types in `proto/`.
- **Eval corpus stays byte-identical** — vendored into the repo as data; the Rust runner ports `run_eval.py`'s contract exactly (fresh temp workdir, fixture never mutated, `verify.sh` inspects repo state only, journal outside workdir).
- **Contract tests remain language-agnostic**: T6 `contract_replay`/`contract_stub.js` still pass; add a Rust-side cage client test.
- Every phase lands its tests in the same commit (the S0–S7 discipline, carried over).
- `cargo test` (unit + lib), `cargo test -- --ignored`-style integration gates, and `tests/` shell scripts for cage/eval jobs that need root-free userns.

---

## 11. Definition of done (acceptance)

- [ ] H0–H7 all shipped, each with green gates
- [ ] `cargo test` + parity fixtures green; `pytest` in curam-security still green (core untouched behavior)
- [ ] `curam evals` runs the 10-task corpus and tallies N1–N10 (task completion ≥ 0.67, ask band [1,5], FP < 5%, etc. — measured, not promised)
- [ ] End-to-end on a real task: TUI + streaming + cage + journal — a human can steer, cancel, approve, and audit everything
- [ ] Every verdict carries `{action, rule_id, source, source_hash, evidence}` in the journal
- [ ] No secrets cross the provider boundary (ported `scenario_pii_scrub` passes)
- [ ] `curam doctor` green on an ollama box and a cloud-key box

---

## 12. Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| curam-core extraction regresses security repo | H0 gate runs the existing full suite before/after; shim is a thin wrapper |
| ratatui API churn between 0.3x | pin exact version in `Cargo.lock` + lockfile; TUI stays a *readers* over agent events (all state in the loop, render-only) |
| Provider SSE variance (some servers don't stream) | identical non-stream code path behind `--no-stream`; read-timeout + heartbeat; degrade loudly, never hang |
| Token/context accounting drifts (N8 ±10%) | keep POC rule: report `usage` fields, log `cost_unknown`, and show ctx fill from local chars/4 approximation — labelled as heuristic |
| Eval parity drift | corpus + verify.sh vendored as data, versioned; runner ports the contract exactly |
| Shell model-fluency risk (the reason V1 wanted typed run) | typed `run` is the default tool; `shell` opt-in per project (`[tools] shell`) and always caged |

---

## 13. Decisions — RESOLVED (defaults taken 2026-09)

1. **Repo:** new `~/code/curam-harness-rs` (standalone).
2. **curam-core consumption:** path dep `../curam-security/crates/curam-core`.
3. **Shell tool default:** `[tools] shell = true` (POC parity, always caged). Typed `run` remains the v1 default path and the V1 target.
4. **Journal:** SQLite-WAL as truth + `--jsonl` export; evlog/JSONL-first deferred (readers later).
5. **TUI default:** auto-TUI when tty.
6. **check_pkg tool in v1:** defer (available in curam-core; not a tool yet).

---

*Appendices (written during implementation, not now): migration notes for POC `curam.toml` → new keys; the frozen `proto/` journal + event schema; the eval corpus README port.*
