# curam — Rust Security Core (used from the Python harness)

**Decision (recorded):** stop polishing the Python POC — its job is proven.
Do NOT rewrite it in Rust wholesale. Instead build the *security layers* in
Rust and have the Python harness call them. Rust earns its place exactly
where the Python version is weakest and where its ecosystem is strongest:

- process sandboxing (extrasafe / Landlock / bwrap / seccomp)
- fast matching over big blocklists (uBlock-Origin-style engine)
- egress enforcement (the injection gap is fundamentally a *boundary* gap)

Everything else (agent loop, journal, budget, skills, UI) stays Python.
The Python harness is frozen for features; it only gains *call sites* into
the security core.

---

## 0. The two Rust artifacts (two answer the "how do we use it" question)

| Artifact | Kind | What it does | How Python uses it |
| --- | --- | --- | --- |
| `curamsec` | PyO3 module (abi3) | in-process: classify commands, scan contexts for injection/secrets, match hostname/path against allow/deny tries, check a dependency against vuln DBs | `from curamsec import classify, scan, match_host, check_pkg` — milliseconds, no IPC |
| `curam-run` | standalone binary | the sandboxed exec wrapper: runs a command inside a cage (bwrap user-ns OR `systemd-run --user` + Landlock/seccomp) with network deny/allow | Python replaces `subprocess.run("bash -c …")` with `curam-run --allow-net=… -- bash -c …` |

In-process matching = the uBlock engine (fast, low latency). Out-of-process
wrapper = the *boundary* (a cage is a process, not a library).

---

## 1. The uBlock-Origin model, mapped onto curam

### How uBO does it (the pattern to copy)

- **Static Network Filtering Engine (SNFE)**: filter *lists* (EasyList, …
  thousands of rules) compiled into **flat arrays + a trie**. Evaluating a
  candidate URL against 100k+ rules is a trie walk, not a regex scan —
  microseconds.
- **Layer semantics**: `allow` (hardcoded defaults) < `block` (filter
  rules) < **exception** rules (`@@||…`) that un-block. Lists refresh
  independently (uBO's "filter lists" updater).

### The curam translation

| uBO concept | curam concept |
| --- | --- |
| filter lists | allow/deny *host* lists; skill/repo signature lists; package vuln lists |
| trie + flat arrays | Rust trie over hostname suffixes + `aho-corasick` automaton for signatures |
| `@@` exception = always-allow | explicit `allow` list (git hosts, registries, build infra) is *always* above block |
| list updater (uBO polls) | `curam lists update` — ETag/If-Modified-Since fetch per list into `~/.local/share/curam/lists/` |
| cosmetic filters | (n/a for a harness) — replaced by *context scanning* (§3) |

**The layered-match call in the harness:**

```
tool_call "bash setup.sh"
  → curamsec.classify(shell_command)      # policy automaton (existing policy.py moved to Rust)
      → "ask" (exec of repo-script signature — the injection fix)
  → curamsec.match_host(url) if network   # trie: block / allow / ask
      → "block" (URLhaus / StevenBlack hit)
  → subprocess → curam-run (cage)          # boundary
```

---

## 2. Open-source data sources (the "databases" — all real, checked 2026)

These play exactly the role filter lists play for uBO — they ARE the blocklist
content, curatable, updatable, and community-maintained.

### Network / malicious hosts & URLs (egress deny)
| Source | Format | Notes |
| --- | --- | --- |
| [StevenBlack/hosts](https://github.com/StevenBlack/hosts) | hosts / dnsmasq | the canonical unified ad+malware host list; ideal trie source |
| [URLhaus (abuse.ch)](https://urlhaus.abuse.ch/api) | CSV / MISP / RPZ | malicious **URLs** (not just hosts) — the "senior" egress rule feed |
| [PhishTank](https://phishtank.org) | CSV | phishing URLs |
| [Pi-hole blocklists](https://pi-hole.net) | lists | huge community corpus already in hosts/dnsmasq format |

### Package / dependency vulnerabilities (the "is this dep known-bad" check)
| Source | Format | Notes |
| --- | --- | --- |
| [OSV.dev](https://osv.dev) | OSV JSON | aggregator: PyPI, npm, crates.io, GHSA, GVD — one API |
| [RustSec advisory-db](https://github.com/rustsec/advisory-db) | OSV JSON | Rust crates exactly |
| [GitHub Advisory DB](https://github.com/advisories) | OSV | used by dependabot; queryable |

### Secret / injection signatures (context scanning)
| Source | Format | Notes |
| --- | --- | --- |
| [detect-secrets](https://github.com/Yelp/detect-secrets) rule set | YAML regex | secret-pattern catalogue (improves our sanitize.py) |
| [gitleaks](https://github.com/gitleaks/gitleaks) rules | JSON | 100+ regex signatures, OSS, license-permissive |
| [prodnull/prompt-injection-repo-dataset](https://huggingface.co/datasets/prodnull/prompt-injection-repo-dataset) | HF | indirect injections in *repo files* — exactly our Gap 1 |
| [AgentInjectionBench](https://huggingface.co/datasets/sincpp/AgentInjectionBench) | HF | multi-step tool-use injection benchmarks |

### "Bad skills / bad agents" — honest answer
There is **no canonical "bad agent skill" database** yet (the space is too
new). What exists: the Agent Skills [registry](https://agentskills.io) (an
allowlist you can trust), and the injection datasets above as *behavioral*
lists. The right structure is therefore **skill trust = your own allowlist
override + optional community signature lists**, curated like uBO's
"my filters" + community lists. This is a *gap in the ecosystem we can fill
later*, not a wait-for-someone-else thing.

---

## 3. Libraries — options per layer (simple first)

### Build / bindings
| Option | Notes |
| --- | --- |
| **Maturin + PyO3** (recommended) | maturin builds+packages the wheel (abi3, cross-version); pyo3 modern (`Bound` API). `uv tool install` / pip both work. |
| cffi (fallback) | no build on import, but manual binding for each call — more effort |
| setuptools-rust | fine, but maturin is less ceremony |

### Matching engine (the uBO core)
| Use | Crate | When |
| --- | --- | --- |
| signatures (secrets, injections, dangerous patterns) | **[`aho-corasick`](https://crates.io/crates/aho-corasick)** — SIMD, streaming, matches-per-byte | all |
| hostname suffix (uBO-style trie) | small custom trie (50 LOC) or the `bm`/`regex-lite` if rules stay simple | hosts lists |
| huge allow/deny sets | `bloom` (pre-filter) then exact | only if lists exceed ~100k |

### Process cage (the boundary)
| Option | Pros |
| --- | --- |
| **[`extrasafe`](https://github.com/khonsulabs/extrasafe)**: unified seccomp + Landlock + user-ns | one crate, high-level, recommended start |
| `rust-landlock` directly | lower level, kernel 5.13+ |
| **bwrap** (system tool, not crate) | battle-tested *unprivileged* sandbox; call out to it from curam-run |
| `systemd-run --user --scope` | *already installed* on Fedora; zero-Rust egress toggle (`PrivateNetwork`, `ProtectSystem`) |

### Egress
- for the **main model HTTP**: check the provider URL with `match_host`
  (in-process) before `urllib` opens it — that's the simple fix.
- for **subprocess egress**: `curam-run` uses bwrap/systemd-run flags
  (`--unshare-net` / `PrivateNetwork=yes`), no Rust networking needed.

### Keep simple — cut
- NO custom DNS resolver (crates: `hickory`/`trust-dns`) — system resolver is
  fine; we match the *requested* hostname, not the IP.
- NO eBPF/aya audit (already cut in curam.md).
- NO WASM/extension tier yet.

---

## 4. Build order (each step end-to-end + a test)

| Step | Artifact | Proves | Test |
| --- | --- | --- | --- |
| **S0** | maturin skeleton, `curamsec.classify("…")->allow/ask/deny` | the hybrid actually works (Rust called from Python) | `curamsec.classify("rm -rf /") == "deny"` |
| **S1** | move policy.py rules into Rust (aho-corasick + wrapper-strip logic) | Rust is the single policy engine | eval `scenario_deny`, `scenario_pii` still pass |
| **S2** | **the injection fix**: `bash <repo-file>` ⇒ ask (exec-of-untrusted-content signature) | the #1 documented security gap flips from leak → blocked | `run_eval --task injection_trap` PASSES |
| **S3** | `curam-run` cage (bwrap/systemd-run) replacing raw subprocess for shell | boundary exists | a net-egress test (wget is blocked; localhost works) |
| **S4** | egress trie: StevenBlack/URLhaus into allow/deny tries; match provider URL + cage flag | uBO-style filtering over real lists | `match_host("evil.example")=="block"`, legit hosts pass |
| **S5** | OSV/RustSec `check_pkg(name,version)` as a tool the agent can call | supply-chain vuln lookup in-loop | offline fixture with a known-vuln package → flagged |
| **S6** | context injection scanner (repo-file → scan before adding to context) | adversarial file loops caught statically | a planted injection in a README gets flagged before the model reads it |

After S2 the injection gap is *closed*; after S4 the firewall decision in
curam.md is *implemented*; S6 is the deepest layer and can be scoped later.

---

## Interfaces & licensing are in the companion doc

Data sources (uBO/Pi-hole usefulness + gaps), the IPC plan (Unix sockets +
versioned wire protocol, not HTTP), and the open-source-first design
principles (licensing, `proto/` contracts, provable verdicts, lists-as-data)
live in **`docs/data-ipc-opensource.md`**. The S0–S6 build order below is
unchanged; that doc fixes the contracts between the pieces.

## 5. What we deliberately keep out of Rust (the restraint)

- The agent loop, journal, budget, skills, UI → stay Python (frozen).
- No rewrite of the whole harness (this doc is the "don't" to that).
- No moving to async Rust for shell calls (bwrap as a child is fine).
- No telemetry layer migration (journal already the source of truth).

## 6. SLO note

This does NOT change the Rust Go/No-Go from curam-evals.md — it *is* the
alternative to "rewrite the whole POC in Rust." If the four security steps
(S0–S4) pass their tests, the security boundary exists in Rust while the
rest of the harness stays proven-Python: the "Rust rewrite" becomes the
"Rust security core + Python shell" instead — which is strictly less work
and keeps the working parts working.
