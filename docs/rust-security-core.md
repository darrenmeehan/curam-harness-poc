# curam — Rust Security Core: the Plan

**Status:** planning complete; **S0 is the next executable step**.
**Decision (recorded in curam.md):** stop polishing the Python POC — its job
is proven. Do NOT rewrite the whole harness in Rust. Build only the
*security layers* in Rust and have the Python harness call them.

This is the single plan. Deep rationale per topic lives in the companion
docs (linked per section); this file is what you implement from.

---

## 0. Why Rust at all (and only here)

Python is weakest / Rust is strongest in exactly three places:

- process sandboxing (the *boundary*)
- fast matching over big blocklists (uBlock-style engine)
- egress enforcement (the injection gap is fundamentally a boundary gap)

Everything else (agent loop, journal, budget, skills, UI) stays Python,
frozen for features — it only gains *call sites* into the security core.

## 1. The two artifacts

| Artifact | Kind | What it does | How Python uses it |
| --- | --- | --- | --- |
| `curamsec` | PyO3 module (abi3, maturin) | in-process: `classify`, `scan_context`, `match_host`, `check_pkg` | `from curamsec import classify, …` — milliseconds, no IPC |
| `curam-run` | standalone binary | sandboxed exec: cage (bwrap), network on/off, country/IP sets | `curam-run --net=allow:github.com -- bash -c …` — ndjson over stdio |

**Line drawn deliberately:** `curamsec` is a *library* (no daemon, no
sockets, no transport — minimum surface). `curam-run` is the only thing
that touches a sandbox/network boundary; its contract is ndjson so any
language can drive it. In-process matching = the uBO engine; out-of-process
wrapper = the boundary (a cage is a process, not a library).

**Architecture + mermaid:** `docs/architecture.md` (also rendered
`docs/img/architecture.svg`).

## 2. The contracts (the seams that must not move)

### Python ↔ curamsec (PyO3, in-process)

```python
classify(cmd: str, *, allow_rules: list[str] | None = None) -> Verdict
scan_context(text: str) -> list[Finding]
match_host(host: str, *, allow: bool = False) -> Verdict
check_pkg(name: str, version: str | None) -> list[Advisory]

class Verdict: action, rule_id, source, source_hash, evidence
```

Every verdict is a **provable chain** `{action, rule_id, source,
source_hash, evidence}` → reproduces from the same lists + rules + list
version. Written to the journal. This is the open-source audit property.

### Python ↔ curam-run (ndjson over stdio, versioned envelope)

```json
{"id":1, "v":"cage/1", "cmd":["bash","-c","cat /etc/passwd"],
 "cwd":"/home/x/project", "timeout_s":30,
 "net":false, "allow_hosts":["github.com"], "deny_hosts":["evil.example"]}
→ {"id":1, "exit":0, "stdout":"…", "cages":["bwrap:unshare-net","ipset:ru"],
   "verdicts":[…]}
```

### curamsec ↔ lists (files on disk, versioned)

```
~/.local/share/curam/lists/
  stevenblack/  urlhaus/  gitleaks/  injection/  allow/
  each: list.json {source_url, license, etag, sha256, refreshed_at, rev, entries}
```

`rev` is **monotonic** (rollback-proofing). `allow/` is highest-trust,
pinned per-release, never auto-refreshed. Everything not in `allow/`
defaults to **ask** — deny lists can only *add* denials.

## 3. Libraries — finalized (no options)

> Deep rationale + rejected alternatives: `docs/rust-libraries.md`.

| Layer | Choice | Because |
| --- | --- | --- |
| Bindings | **maturin + PyO3** (abi3) | one command → wheel; manylinux CI; Apache-2.0 |
| Matching | **aho-corasick** (signatures) + custom hostname trie (~100 LOC) | uBO-shaped; linear-time, SIMD |
| Cage | **bwrap** (system tool, zero crate) for v1 | most-proven unprivileged sandbox; extrasafe (seccomp+Landlock+userns) *only if* we later embed the cage |
| Child mgmt | **sandbox_command** | routes child through the sandbox, leaves IO to us |
| CLI | **clap**; config via plain JSON from Python | few args; no TOML dep needed |
| DNS | **none — no hickory** | we match *requested hostnames*, not IPs; system resolver is ground truth. See §5. |
| Async | **none** — tokio/axum only when a server artifact appears (phone-bridge daemon); contracts stay transport-agnostic | not a server; stdlib threads; see rust-libraries.md |

**Deliberately cut:** hickory/trust-dns, eBPF/aya, Wasm tier,

bloom (only if >100k entries), processkit (escalator only), TUF machinery
(a monotonic `rev` suffices).

## 4. Data sources (the "databases")

> uBO/Pi-hole: what they help with + their gaps: `docs/data-ipc-opensource.md` §1.

| Layer | Feeds | Treatment |
| --- | --- | --- |
| Egress deny | StevenBlack (hosts), URLhaus (URLs), PhishTank | pinned snapshot + hash; tamper can only *add* denials |
| Dev-infra **allow** | **ours**: git hosts, registries, build infra | signed per-release, pinned in-repo, never auto-refreshed |
| Package vuln | OSV.dev, RustSec, GHSA | `check_pkg` tool the agent can call (S5) |
| Secret/injection signatures | detect-secrets, gitleaks rules + injection datasets (prodnull, AgentInjectionBench) | aho-corasick automaton in `scan_context` |
| **Bad skills/agents** | **no canonical DB exists** — skill trust = *our allowlist override* + optional community signature lists | a gap we can fill later; do not wait on it |

## 5. IP / country blocking (e.g. "block all of Russia")

**Not a resolver question, not in curamsec.** Country sets (100k–1M CIDRs)
are enforced at the **network boundary** of the cage (netns + nftables/ipset
sets via `geo-nft`/`ipset-country`) — kernel-enforced at *the actual
connection*, no TOCTOU, O(1)-ish at scale. The harness still matches
hostnames only; `match_host` stays string-only.

- `block Russia` = an ipset rule on the cage's netns; `curam-run` already
  provides the netns (`--net`). Zero extra Rust.
- **OSS licensing caveat:** MaxMind GeoLite2 is free but **not
  redistributable**; DB-IP Lite (CC BY 4.0) / IP2Location LITE are the
  permissive picks. curam feeds the CIDR source to nftables; the DB never
  lives in the repo.

## 6. Feed-pull security (how we pull the lists safely)

> Full threat model + controls: `docs/data-ipc-opensource.md` §4.

Most feeds can't sign their own content (StevenBlack = git repo, URLhaus =
CSV), so the scheme is layered, not one signature. Threat model: MITM /
DNS hijack, feed-host compromise, local cache tamper, rollback replay.

**The controls (in order):**

1. **Pin a snapshot, never poll `HEAD`.** `curam lists update <list>`
   targets `repo@commit`/`@rev`; bumping the pin is a deliberate,
   reviewable step. Kills feed-host-compromise.
2. **Content-addressing + atomic swap.** Fetch into staging, verify
   `sha256`, atomic `rename` into place. A tampered/half-downloaded list
   never becomes visible.
3. **Verify at load, not only at fetch.** `curamsec` checks on-disk
   hash+signature every load. Covers offline + local cache tamper.
4. **Monotonic `rev` = rollback-proof.** A list with `rev` ≤ last-seen is
   rejected. One integer, no TUF.
5. **Allow/deny asymmetry (why unsigned deny feeds are safe to consume):**
   deny tamper = worst case a DoS; allow tamper = grants reachability. So
   `allow/` is signed per-release + pinned; deny are pinned + hashed; and
   "unknown ⇒ ask" means deny can never silently *grant*.
6. **Sign the mirror, not the upstream** (later, when we publish feeds):
   an age key held **out-of-band** (not on the agent box) signs snapshot
   manifests. Default posture needs *no keys*: pin+hash+rev+load-verify
   already defeat MITM, feed compromise, cache tamper, rollback.
7. **The updater's own supply chain:** the tool that pulls feeds ships via
   a **pinned Nix flake** (same lesson as the pi/npm audit). Update by
   bumping the lock.

**Ladder:** minimum (pin+hash+atomic+load-verify+rev) ships with S0–S4;
signing when we publish public feeds; **never** auto-refresh deny at PAR
with no pin — that's the uBO/Pi-hole default we're deliberately not copying.

## 7. IPC: local vs remote

- Python ↔ curamsec: **in-process (PyO3)** — no IPC at all.
- Python ↔ curam-run: **ndjson over stdio** — protocol testable without
  our code (the OSS property).
- Future daemon (supervisor/telemetry/phone bridge): **AF_UNIX +
  JSON-RPC 2.0-style** in `$XDG_RUNTIME_DIR/curam/` (per-user, perms =
  auth, auto-cleaned). Systemd socket activation / LSP-style.
- **HTTP only for genuinely remote** (the phone bridge over Matrix/
  Tailscale). Same message schema, different transport.
- The **journal is the shared event stream** — any component tails the
  append-only JSONL; no new transport for consumers.

## 8. Build order (each step end-to-end + tests + feed-security progress)

Tests T1–T7 are defined in `docs/architecture.md` §3; each step lands its
tests. Feed-pull security controls land *with* the step that first needs
them — not bolted on later.

| Step | Builds | Security requirement landed | Proves (test) |
| --- | --- | --- | --- |
| **S0** | maturin skeleton; `curamsec` crate with `classify("…")→allow/ask/deny`, `list.json` type incl. monotonic `rev` | verify-at-load skeleton + manifest type from commit one | `classify("rm -rf /")=="deny"` (T1) |
| **S1** | move policy.py rules into Rust (aho-corasick + wrapper-strip) | — (port must be behavior-identical) | `scenario_deny`, `scenario_pii` still pass (T2) |
| **S2** | **the injection fix**: `bash <repo-file>` ⇒ ask (exec-of-untrusted-content signature) | — | `run_eval --task injection_trap` **PASS** (was FAIL) — closes Gap 1 |
| **S3** | `curam-run` cage (bwrap) replacing raw subprocess; netns on/off; **geo sets (ipset/nftables) wired into the netns** | the boundary + country/IP layer exist | egress denied (`wget example.com`), localhost allowed, `rm /etc/passwd` denied, timeout→`timed_out` (T5) |
| **S4** | `curam lists update` (pin+hash+atomic swap+load-verify+`rev`), egress tries from StevenBlack/URLhaus + our `allow/` | **feed-pull security §6 minimum** | `match_host("evil.example")=="block"`, legit hosts pass; tampered list rejected at load (T3) |
| **S5** | OSV/RustSec `check_pkg(name, version)` as a callable tool | — | offline fixture: known-vuln package flagged, clean → `[]` (T4) |
| **S6** | context injection scanner (repo-file → scan before context) | — | planted README injection flagged before the model reads it (becomes a T7 gate) |
| **S7** *(later, only if we publish public feeds)* | snapshot-manifest signing (out-of-band age key), mirror | §6 item 6 | signature verify + rollback over a mirrored feed |

**Milestones:** after S2 the injection gap is *closed*; after S4 the curam.md
firewall decision is *implemented* and feeds are pulled safely; S3 already
carries the "block all of Russia" capability; S6 is the deepest layer, can be
scoped later.

**S0–S6 are the Go/No-Go** from `curam-evals.md`: pass S0–S4 with their
tests and the security boundary exists in Rust while the rest stays
proven-Python — "Rust security core + Python shell", strictly less work than
a full rewrite.

## 9. What stays Python (the restraint)

- Agent loop, journal, budget, skills, UI → frozen Python.
- No whole-harness rewrite, no async Rust, no telemetry migration.
- Python gains *only* call sites into `curamsec` / `curam-run`.

## 10. Open-source-first (baked in from day one)

> Full principles: `docs/data-ipc-opensource.md` §3.

1. **License per artifact:** `curamsec` Apache-2.0/MIT; lists only from
   permissive sources (never vendor URLhaus or GeoLite).
2. **Protocol-first:** contracts above live in `proto/`, versioned,
   reviewed like code; testable by contributors in any language (T6).
3. **Provable verdicts:** every decision logs the full chain
   `{verdict, rule_id, source, version, evidence}`.
4. **Lists as data:** pinned, versioned in-repo; community PRs list
   entries, not code.
5. **Decoupled artifacts** (crate / binary / lists tool / harness) — each
   standalone, each swappable.
6. **CI + hygiene day one:** maturin wheel on GH Actions, `uv sync` for
   dev, conventional commits, SECURITY.md, no secrets (gitleaks already
   wired).
