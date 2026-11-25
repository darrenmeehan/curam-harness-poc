# The curam repos — what each one is, and why they are separate

Four repositories hold curam. This page is the only place that describes the
set as a whole; each repo's own README covers that repo.

If you are cloning for the first time, read the table, then the dependency
graph, then "Known friction". The friction section is the honest part.

## 1. The four repos

| Repo | Role | Language | Status |
| --- | --- | --- | --- |
| `curam-harness-poc` | The Python POC **and the design docs of record** | Python, stdlib-only | Frozen — proved the risky bits, not production |
| `curam-security` | The Rust security core: one library, three consumers | Rust | Active |
| `curam-harness-rs` | The Rust harness, built on the POC's semantics | Rust | Active |
| `pi-curam-security` | The boundary engine's **drivers** (pi, and later Claude Code / Codex) | TypeScript | Active |

### `curam-harness-poc` — the POC and the design of record

The original Python harness. It answered three questions with evals
(`docs/curam-evals.md`): can an LLM do real work through these tools with a
shell; can we tell when to ask the human; does the two-stage classifier plus
sanitizer plus policy catch the ugly stuff.

It is deliberately frozen. Its lasting value is `docs/`: `curam.md` (vision and
decisions), `curam-slos.md`, `curam-diagrams.md`, `rust-harness-plan.md`,
`rust-security-core.md`, `architecture.md`, `REPOS.md` (this page). The
architecture of record lives here, next to the POC that produced it — not in
the repo that ships it.

### `curam-security` — the core, and the binaries that wrap it

A Cargo workspace with four members:

| Member | Kind | What it is |
| --- | --- | --- |
| `crates/curam-core` | library | Pure-Rust detection logic: `classify`, `scan_context`, `match_host`, `check_pkg`, scrubber, verified lists. No FFI, no Python, no I/O policy. |
| `crates/curamsec` | pyo3 cdylib | The abi3 wheel surface over `curam-core` — what the Python harness imports. |
| `bin/curam-run` | binary | The bwrap cage. `cage/1` NDJSON over stdio. |
| `bin/curam-membrane` | binary | The boundary engine. `mem/1` NDJSON over stdio. |

Why it is one repo: these four share the anti-drift rule — *no detection logic
lives outside `curam-core`* — and that rule is only enforceable when the core
and everything that wraps it are in one build. Three consumers means three
different packaging stories (pyo3 wheel, Rust lib, stdio binary), which is
exactly why they are four workspace members rather than four repos.

### `curam-harness-rs` — the Rust harness

The POC rewritten in Rust, semantic-for-semantic. TUI (ratatui), agent loop,
provider client, tools, journal, budget, SLI, skills. It links `curam-core`
directly — no FFI, no duplication.

### `pi-curam-security` — the drivers, not the engine

The engine lives in `curam-security` (`bin/curam-membrane`). This repo holds the
**drivers**: thin adapters that sit inside a host harness (pi first) and forward
tool results to the engine over `mem/1`. The plugin is a dumb pipe plus thin UI;
all detection stays in the engine. `PLAN.md` here is the design for the engine's
v1 surface. The repo was called `curam-membrane` until it collided with the
engine's name — see 3c.

## 2. The dependency graph

Who needs whom at build time and at run time:

```
  curam-harness-poc  (Python)
        |  imports the curamsec wheel      (built in curam-security)
        |  spawns  curam-run               (built in curam-security)
        v
  curam-security  (Rust workspace)
        ^  curam-core  <-- path dep --  curam-harness-rs  (Rust)
        |  ^
        |  +-- spawns curam-run
        |
        +-- curam-membrane engine <-- stdio mem/1 --  pi-curam-security  (TS drivers)
```

Build order matters: `curam-security` first. The wheel and both binaries come
from there. `curam-harness-rs` additionally needs `curam-security` checked out
as a **sibling directory** — see the first friction item.

## 3. Known friction

These are real and currently unfixed. Read them before restructuring anything.

### 3a. `curam-harness-rs` cannot build from a fresh clone

```toml
# curam-harness-rs/Cargo.toml
curam-core = { path = "../curam-security/crates/curam-core" }
```

The path escapes the repository. A fresh `git clone curam-harness-rs` followed
by `cargo build` fails: the sibling does not exist. You must clone
`curam-security` next to it, at exactly that relative path. This is the single
strongest argument that the two belong in one workspace — they are already one
build unit, just not one repository.

Verified on a clean clone:

```
Unable to update /tmp/curam-security/crates/curam-core
  failed to read .../curam-core/Cargo.toml
  No such file or directory (os error 2)
```

`curam-harness-rs` also has no `flake.nix` of its own, so it has no devShell to
get a toolchain from — unlike `curam-security`, which does. Building it means
borrowing another repo's shell (`nix develop ~/code/curam-security`) or having a
global Rust toolchain, which managed machines deliberately do not have. That is
a second, independent reason the two belong together.

### 3b. The scrubber is duplicated, against the stated rule

`curam-harness-rs/src/sanitize.rs` carries a byte-identical copy of the
`PATTERNS` table in `curam-security/crates/curam-core/src/scrub.rs`. The core's
own header says:

> anti-drift rule: ALL detection lives in curam-core

and `pi-curam-security/PLAN.md` §3 states the same rule in bold. The duplicate
predates the rule being written down. `PLAN.md` §4 plans to "promote wholesale
into `curam-core`" — the promotion happened; the harness copy was never
deleted. Until it is, the rule is aspirational rather than enforced.

### 3c. The repo/engine name collision (resolved)

This used to be the worst of the four: `curam-membrane` (repo) contained
drivers, while `curam-membrane` (binary) was the engine and lived in
`curam-security`. The repo was renamed to `pi-curam-security` to end that.

Two smaller oddities remain. The extension the repo ships is still called
`curam-membrane-pi` (and the `curam-membrane` flag in nix-config's
`pi-config.nix` still gates it). And the repo plans drivers for `claude` and
`codex` as well as `pi`, so the `pi-` prefix under-describes what it will hold.
Neither is urgent; both are cheap to change while the repo is young.

### 3d. The design docs sit in the repo labelled least trustworthy

`curam-harness-poc/README.md` says "Not production. No sandbox — that's phase 2 on
purpose." That is the repo whose `docs/` contains the architecture of record.
The label is accurate for the *code* and misleading for the *documentation*.

## 4. Should these be fewer repos?

Short answer: **yes, one merge is clearly justified.**

`curam-security` + `curam-harness-rs` — merge. The path dependency means the
harness already cannot build without the core, and the duplicated scrubber
shows the anti-drift rule failing exactly as predicted. Making the harness a
workspace member of `curam-security` (or a subdirectory) removes the sibling
checkout requirement, makes the rule enforceable by the compiler, and drops the
repo count to three. The counter-argument is release coupling: the pyo3 wheel
and a TUI application would ship from one workspace. That is a workflow
preference, not a technical blocker — Cargo builds per-package, so `cargo build
-p curamsec` would not pull in the TUI.

`curam-harness-poc` (Python) — keep separate. It is frozen, it is the design of
record, and it consumes the core through a wheel rather than a path
dependency. Merging it would drag a Python POC into a Rust workspace for no
build-time benefit.

`pi-curam-security` (TS drivers) — keep separate. Different language, different
host, different release cadence. It talks to the engine over a frozen protocol,
which is the correct boundary.

**Resulting target: three repos.** `curam-harness-poc` (Python POC + design docs),
`curam-security` (core + engine + Rust harness), `pi-curam-security` (TS drivers).

This merge has not been performed. It is recorded here as the recommendation so
the decision is not lost. If you disagree, change this section — that is what
it is for.

## 5. Where to look next

| Question | File |
| --- | --- |
| What is curam trying to be? | `curam-harness-poc/docs/curam.md` |
| What does the architecture look like? | `curam-harness-poc/docs/architecture.md`, `curam-diagrams.md` |
| What are the contracts? | `curam-security/proto/ndjson-spec.md` |
| What is the Rust core plan? | `curam-harness-poc/docs/rust-security-core.md` |
| What is the Rust harness plan? | `curam-harness-poc/docs/rust-harness-plan.md` |
| What is the engine's v1 design? | `pi-curam-security/PLAN.md` |
