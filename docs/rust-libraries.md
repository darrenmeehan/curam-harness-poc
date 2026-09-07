# curam — Rust libraries, explained

Why we'd use each crate (and what the alternative is). The rule: pick the
**smallest thing that does the job**; if two are equal, take the one that's
correct on the security edge case. This is the "options to choose from"
reference — finalizes what the S0–S6 steps actually `cargo add`.

---

## A. Bindings: Maturin + PyO3

| | |
|---|---|
| **What** | Maturin builds a wheel directly from Rust; PyO3 is the Python↔Rust binding layer. |
| **Why it** | One command (no `setup.py` glue); `abi3` wheels mean **one binary for all Python 3.x**; supports manylinux/musllinux for GitHub-actions CI; Apache-2.0. |
| **The alternative** | setuptools-rust (more build ceremony), cffi (per-call manual bindings, more effort, no static typing). |
| **Start simple** | A single `curamsec` module exposing `classify`, `scan_context`, `match_host`, `check_pkg`. This is a **library**, not a service — no sockets, no daemon. |

---

## B. Matching engine: `aho-corasick` (+ a small custom trie)

| Crate | When |
|---|---|
| **aho-corasick** | the workhorse. Builds a single automaton over thousands of signatures (secret regexes, injection phrases, dangerous-script markers) and matches in linear time. SIMD-accelerated; handles overlap, case-insensitive. |
| **custom hostname trie** | ~100 lines of Rust over suffix segments (`.coname`, `.tld`); PSL-style suffix matching, the uBO-shaped structure for `allow`/`deny`-host tries. Kept custom because a byteslice trie is trivial and keeps deps to one. |
| **bloom** (only if >100k entries) | pre-filter before exact match. Not needed at POC scale — add when a real list demands it. |

---

## C. Process cage: the "boundary" (where the security actually lives)

Three transport options, same goal — run an untrusted command so it cannot
touch the host or the network except what we allow:

| Crate / tool | What it actually does | When to pick |
|---|---|---|
| **extrasafe** | one crate: seccomp (syscall allowlist) + Landlock (file-tree allow) + user-namespace isolation for the child | recommended — single dependency, modern API; needs Linux 5.13+ (fine: this is a Linux harness) |
| **rust-landlock** (direct) | Landlock only — best if we want file-tree-only and let bwrap handle namespaces | keep as an option if extrasafe proves too heavy |
| **bwrap** (system tool, not a crate) | the battle-tested *unprivileged* container: `bwrap --unshare-net --ro-bind ... -- chroot/exec` | zero Rust; use it from `curam-run` and skip the crate entirely — the lazy option |
| **systemd-run --user --scope** | `PrivateNetwork=yes, ProtectSystem=...` per-invocation | already on Fedora, zero deps, but tied to systemd |

**The honest recommendation:** `curam-run`'s first version calls **bwrap**
(no crate at all — most proven, least code). Add `extrasafe` only if we
later want to embed the cage *inside* the binary (reduce moving parts).
The test seam is the same either way: a `run_caged()` function whose only
contract is the result struct.

| | |
|---|---|
| **hickory-resolver** | in-process DNS (recursive, /etc/resolv.conf). We do **not** use it — see "IP / country blocking" below for why. Cut it. |
| |
|---|---|
| **IP / country blocking** (e.g. *block all of Russia*) | This is **not** a resolver question. Country/CIDR sets — 100k–1000k ranges — are enforced at the *network boundary* (nftables/ipset `geo-nft`/`ipset-country`), *never* in the match path. So the harness still matches hostnames only; the cage's egress netns carries the geo-set. See below. |

---

## D. Process/child management (curam-run's job, not a framework)

| Crate | Use |
|---|---|
| **sandbox_command** | `Command` builder that routes a child through the host sandbox (bwrap/systemd-run), leaves IO to us — exactly the shape `curam-run` wants. |
| **processkit** | process-tree lifecycle (kill-on-drop, group). Simple lifecycle mgmt on stdlib is fine for v1; add processkit only if a runaway child becomes a real incident. |

We use `sandbox_command` for v1; `processkit` is the escalator, not the
default.

---

## E. Everything else: stdlib first

- JSON: `serde_json` (optional — stdlib json could do, but serde is the
  Rust convention and makes the `proto/` schema types clean).
- CLI: `clap` (small; our args are few) — we already listed `clap`/`rigel`
  for the Rust V1; same chips here.
- TOML parse of `curam.toml`: `toml` crate (or keep config Python-side and
  pass a plain JSON blob cross-language — simpler).
- DNS: **no crate** — see B.
- Concurrency: default OS threads / stdlib. No tokio dependency for the
  security core (it's not a server).

---

## F. The full dependency set (final)

```toml
# curamsec (library)            # curam-run (binary)
maturin/pyo3                    # sandbox_command
aho-corasick                    # (bwrap invoked as a subprocess)
serde + serde_json              # serde_json (for the ndjson result)
# (custom hostname trie = our code)   # clap (argv)
```

Ten crates max, most stdlib-or-self. That's the "as simple as possible"
target: **no async runtime, no framework, no daemon, no DNS crate** — the
pieces that justify Rust here are the trie/automaton (B), the boundaries
(C), and the PyO3 surface (A).

---

## G. What each library test looks like (they were chosen to be testable)

| Layer | Test (cheap, no live model) |
| --- | --- |
| aho-corasick matcher | golden vectors: 10k signatures → correct match set; perf sanity (μs) |
| hostname trie | PSL edge cases: `evil.example`, `sub.evil.example`, `example.co` NOT blocked |
| extrasafe/bwrap cage | egress test: `wget http://example.com` → denied in cage; `curl localhost` → allowed; `rm /etc/passwd` → denied |
| curamsec PyO3 | `import curamsec; curamsec.classify("rm -rf /")=="deny"` — a real Python test, not just Rust |
| IPC/ndjson | a tiny stub consumer re-implements the ndjson contract (proves the *protocol* is testable without our code) |

That last one is the important open-source property: the contract is testable
by someone who doesn't use our stack.


## H. IP / country blocking lives at the network boundary, not the resolver

**(Semi-)Question: "should we use hickory? what if we block IPs — e.g. all of Russia?"**

Answer: **no hickory** — and country blocking is a *separate layer* from DNS.

### Why we don't put it in the app's DNS path

- Country sets are 100k–1M CIDR ranges (Russia alone ≈ 15k blocks). Matching
  those *per request in the harness* is slow and pointless — the kernel does
  it (nftables `set`/`ipset` indexed, O(1)-ish) in the cage's netns.
- Even if we resolved + checked in-process, there's a **TOCTOU gap**: the
  verdict happens at request time, but the *actual* connection happens
  later — the app could re-resolve. The kernel enforces *the* connection.
- The `getaddrinfo` the child actually uses is the system resolver; hickory's
  in-process resolver is one more place to drift from reality.

### What we DO put in

| Layer | Where | What it blocks | Notes |
|---|---|---|---|
| `curam-run` cage (bwrap `--unshare-net` + netns) | **network boundary** | country/IP *sets* | `geo-nft`/`ipset-country` pull ipdeny/ipverse ranges → nftables set. ipset is the tool; the crate is not involved. |
| `curamsec.match_host()` | in-process, pre-connect | hostname *reputation* (StevenBlack, URLhaus, allow-list) | stays string-only — no DNS, no IPs. |

So: `block Russia` = an nftables/ipset rule on the cage's netns; the harness
still only matches hostnames. **Nothing to do in Rust** — this is system
tooling, not curamsec. Documented in architecture.md § 2c.

### OSS-license caveat on the *data* (for "open-source this later")

- **MaxMind GeoLite2** is *free* but requires registering + acceptance of
  GeoLite EULA; **not redistributable** in the db → cannot be vendored into
  an OSS repo.
- **DB-IP Lite** (CC BY 4.0) and **IP2Location LITE** (attribution) are the
  permissive picks for shipping a country feed with the project.
- Curam only *feeds the CIDR source* to nftables; we don't ship the db.
