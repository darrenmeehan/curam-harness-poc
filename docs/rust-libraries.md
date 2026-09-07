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
| **hickory-resolver** | in-process DNS (recursive, /etc/resolv.conf) — we do **not** need this: the POC matches the *requested hostname*, not the IP resolution, and `getaddrinfo` via stdlib is fine. Cut it. |

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
