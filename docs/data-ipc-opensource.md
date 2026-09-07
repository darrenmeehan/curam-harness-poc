# curam — Data Sources, IPC, and Open-Source-First Design

Companion to `rust-security-core.md`. Answers three questions the earlier
plan left implicit:

1. Do uBlock / Pi-hole databases help us? (and what gaps do they leave?)
2. What is the right cross-process communication for a machine-local
   ecosystem (beyond HTTP)?
3. We may open-source elements — what does the architecture need from day
   one to make that clean?

---

## 1. uBlock / Pi-hole data: useful, but they're *ad-domain* lists, not
   *development-trust* lists

### What they ARE (the data + format)

| | uBlock Origin | Pi-hole |
| --- | --- | --- |
| core mechanism | client-side filter engine (SNFE: trie + flat arrays) | DNS sinkhole (gravity pulls lists → resolves blocked → null) |
| list language | Adblock-Plus (ABP): `\|\|domain^`, `@@` exemptions, `$` options | hosts format (`0.0.0.0 domain`) + plain domain lists (Firebog/Tall, StevenBlack, Mahakala) |
| shared input | **Public Suffix List** (PSL) for suffix-aware matching | same |
| aim | block ads/trackers/malware *domains+URLs* | block ads/trackers/malware *domains* at DNS |

So both are **network-reputation deny-lists for ad/malware domains**, sharp
at exactly one thing: "is this host likely ad/tracker/malware?"

### Where they genuinely HELP curam

- **Egress deny feed** for `curam-run` (the cage): if the harness follows a
  poisoned instruction, `wget evil.example` → the run's egress list
  (StevenBlack hostnames, URLhaus URLs) blocks *delivery* of the C2/exfil.
  That's the "structural boundary, not model judgment" the injection gap
  needs.
- **Freshness + updater pattern**: their list-refresh model (pull feeds,
  ETag/If-Modified-Since, per-list metadata) is exactly the `curam lists
  update` design we already sketched.
- **The trie engine**: uBO proves the data structure scales (100k+ rules,
  microsecond matches). We reuse the *shape*, not their binary.

### The gaps — why they are NOT sufficient for us

1. **They're ad-centric deny; we need dev-infra *allow*.** No ad list knows
   that `files.pythonhosted.org`, `crates.io`, `registry.npmjs.org`,
   `maven-central`, `github.com`, `ghcr.io`, `objects.githubusercontent.com`
   must be reachable for the agent to *do its job*. A software supply-chain
   allowlist (the thing the curam.md firewall decision already calls for) is
   almost entirely *additive* — the uBO/Pi-hole lists don't provide it.
2. **Zero-day lag, high cost of false positive.** A freshly-spun-up
   malicious host (the realistic agent attack) is *not yet listed* anywhere
   — blocklists are a floor, not complete. And in our domain a false
   positive doesn't cost an ad impression, it **breaks a build/dependency
   fetch**. So: block on list is fine, but "unknown" must default to
   **ask/cage** — never hard-block legitimate dev infra on a stale list.
3. **Reputation ≠ content trust.** They encode "this domain bad", not
   "this repo/skills file is adversarial" — which is exactly our Gap 1
   (injection lives in *content*, not the network path). The scanners
   (detect-secrets/gitleaks/injection datasets) and the exec-policy rule
   (repo-file → exec = ask) cover content; the host lists cover delivery.
4. **Format/locence sprawl.** hosts / ABP / dnsmasq / RPZ / CSV / MISP /
   JSON; different cadences; URLhaus is fair-use (commercial tier paid).
   Needs a normalization layer + per-list metadata (URL, license, etag,
   hash) + the journal records *which list version* produced a verdict, so
   every decision is auditable.
5. **Package-vuln ≠ host-block.** OSV/RustSec/GHSA answer "is this
   considered-bad *package*", an entirely different axis lists don't touch.

**Verdict:** use them as the egress deny *feed*, layer our own
supply-chain allowlist *above* them (allow is always $ higher), and treat
every list as a *versioned, auditable input* — not ground truth.

---

## 2. Inter-process communication on one machine: Unix sockets + a
   versioned wire protocol, not HTTP

### What we actually have to talk

| A → B | Today (POC) | Right talk |
| --- | --- | --- |
| Python harness → `curamsec` | PyO3 in-process call (no IPC at all) | keep in-process — milliseconds, no serialization |
| Python harness → `curam-run` (cage) | `subprocess.run` | **newline-delimited JSON (ndjson) over the child's stdin/stdout** — streaming output, cancellation, structured results, no new protocol |
| future daemon (supervisor, phone bridge, telemetry) | — | **Unix domain socket (AF_UNIX), JSON-RPC 2.0-style** messages, LSP-style |

### Why not HTTP for the machine-local parts

- HTTP/TCP brings ports, network namespaces, TLS decisions, discovery —
  all *unnecessary state* when both ends are the same user on the same box.
- **Unix domain sockets** are the OS-native answer to "processes on one
  machine": stdlib `socket.AF_UNIX`, no port collisions, filesystem
  permissions are the auth (mode 0700 dir = only you), zero-config, and it
  is what the *ecosystem* already does (systemd socket activation, LSP
  servers, Docker/Postgres local sockets).
- **Where to put them:** `$XDG_RUNTIME_DIR/curam/` (per-user, tmpfs,
  auto-cleaned by systemd) — never a stale file in /tmp or $HOME.
- **The one honest case for HTTP:** the *phone bridge* (Matrix/Tailscale)
  — that's genuinely remote. Local: UDS. Remote: HTTP/Matrix. Same message
  schema, different transport.

### The wire protocol (open-source-first — this is the contract)

- **ndjson over stdio** for the per-command cage call: one JSON object per
  line (request in, {stdout,stderr,exit,events} out). Trivial to reimplement
  in any language — that's the point.
- **JSON-RPC 2.0-ish over UDS** for the daemon surface (once one exists):
  `{"id","method","params"}` / `{"id","result"|"error"}`. Editors, CLIs,
  and other agents can speak it without owning our Python.
- **The journal stays the event source of truth**; readers *tail* it
  (append-only JSONL already). Any component can be an event consumer
  without a new transport.
- **Version the protocol, not the code:** `curam/ipc/v1` envelope fields,
  a `proto/` directory with JSON-Schema/OpenAPI for anything HTTP, so
  consumers pin a version instead of our internals.

### When to keep it even simpler

- One-shot command → subprocess+ndjson is *enough*. Don't build a daemon
  until a second consumer needs it.
- curamsec is a *library* (PyO3), not a service — no socket at all.

---

## 3. Open-source-first: what "built with this in mind" means concretely

1. **License the pieces that can ship independently.**
   - `curamsec`: Apache-2.0 (crates.io convention, and dual-license
     MIT/Apache like many Rust projects).
   - lists/data: re-derive from their *permissive* sources (StevenBlack is
     MIT; gitleaks is MIT; detect-secrets MIT; OSV/RustSec data are open) —
     never vendor a fair-use/commercial feed (URLhaus) into the repo.
2. **Protocol-first, internals-second.** The IPC schema + the optional HTTP
   OpenAPI live in `proto/` and are reviewed like code. A contributor adds
   a consumer in another language without touching our Python.
3. **Every verdict is a data chain, not a bare bool.**
   `{verdict, rule_id, source, source_version, evidence}` — from
   `curamsec.classify` / `match_host` / `check_pkg` straight into the
   journal. An attacker-turned-auditor (or a skeptic) can reproduce any
   block/allow from the same lists + rules. This is the single most
   valuable open-source property: *provable verdicts*.
4. **Lists as data, versioned in-repo.** `lists/` with `list.json`
   (source URL, license, refresh policy, sha256). Community maintainers
   PR list *entries*, not code; admins "pin a list version" per project.
5. **No code/data coupling:** curamsec crate builds standalone; curam-run
   is a self-contained binary; the normalize+refresh job is a `lists`
   subcommand; the Python harness is one consumer. Anyone can swap in
   their own harness/shell around the same core.
6. **CI + hygiene on day one:** maturin wheel build on GH Actions, `uv
   sync` for dev, conventional commits, SECURITY.md, ADR-style decisions
   (we already do), no secrets (sanitize + gitleaks already run).

---

## 4. Securing the feed pull (ingestion threat model)

Most feeds **cannot** sign their own content (StevenBlack = git repo,
URLhaus = plain CSV, OSV = aggregate API). So "secure the pull" is a layered
story, not a single signature:

### Threat model

| Attacker | Can do | Our blunt |
| --- | --- | --- |
| MITM / DNS hijack on the way down | serve a swapped list | HTTPS + **content hash** (don't trust the pipe) |
| feed host itself compromised | serve a swapped list (with a valid cert) | **pin a version + mirror + sign** the snapshot; never trust live upstream |
| local tamper of our cache | replace a stored list | **verify at load, not only at fetch** |
| rollback (replay an older *good* list) | re-enable a known-bad host / drop a fix | **monotonic** `rev`/timestamp in the manifest |

### The controls, in the order that matters

1. **PIN a version, don't poll live.** `curam lists update` targets a
   **pinned snapshot** (`source repo@commit` / mirror `@rev`), not `HEAD`.
   Bumping the pin is a deliberate, reviewable step — the "refresh" that
   uBO/Pi-hole do automatically is done here by an explicit `curam lists
   update <list>` after we look at the upstream diff. This single change
   kills the "feed host compromised" row.
2. **Content-addressing + hash verify.** `list.json` already carries
   `sha256`; the updater fetches into a *staging* dir, verifies the hash
   against the manifest, then **atomically swaps** (rename) into place.
   A half-downloaded or tampered list never becomes visible.
3. **Sign the mirror, not the upstream.** We can't force upstream to sign,
   but *we* sign the pinned snapshot manifest with a curam project key
   (age/minisign/SSH). The jar is now "trust TLS to reach our mirror +
   trust our signature over the snapshot" — narrower than "trust the whole
   feed host forever."
4. **Verify at load, not only at fetch.** `curamsec` checks the on-disk
   `list.json` hash+signature every time it loads a list. Covers offline
   use and local cache tamper.
5. **Rollback proof.** Manifest has a monotonic `rev`; a loaded list with a
   `rev` ≤ the last seen is rejected (a real TUF-style anti-rollback, one
   integer, no TUF machinery).
6. **The asymmetry that simplifies everything** (from data-ipc §1):
   the **allow list is the high-value target** (it *grants* a host/path
   reachability); the **deny lists are low-value** (worst case a DoS). So:
   - **allow/** → highest trust: signed per-release, pinned in-repo,
     updated only via release (reviewed, mirrored). Never auto-refreshed.
   - **deny/ feeds** (StevenBlack, URLhaus, gitleaks, injection signals) →
     pinned snapshot + hash; a tampered deny can only *add* denials, and
     "ask on unknown" means it can never silently *grant*.
   This is why the "allow always wins / unknown ⇒ ask" default is not just
   semantics — it's the security property that makes unsigned community
   deny-feeds *safe to consume*.
7. **The updater's own supply chain.** The tool that pulls feeds, and its
   build (maturin wheel, deps), ships via a **pinned Nix flake** — the same
   lesson as the pi/npm audit (your nix-config pin/hash). Update the
   updater by bumping the lock, not `pip install --upgrade`.

### Where keys live / a caveat

- Signing key for curam snapshots: **age key held out-of-band** (not on the
  24/7 agent box), so a local compromise of the harness can't re-sign a
  tampered snapshot. If the key is on the same box as the agent, signatures
  protect the *transport/mirror*, not a root-level local attacker — be
  honest about that in the threat model.
- Default posture, no keys configured: **pin + hash + monotonic rev +
  load-time verify** still apply and already defeat MITM, feed-host
  compromise, cache tamper, and rollback. Signatures are the *optional add*
  for the mirror story. Build the pinned+hashed+monotonic core first; sign
  the mirror when we publish public feeds.

### Minimum → later ladder
- **Minimum (ship S0–S4):** pin + sha256 verify + atomic swap + load-time
  verify + monotonic rev. No new infra.
- **Later (publishing our mirror):** project signing key for snapshot
  manifests; TUF-style minimal if we ever become a *directory* of feeds.
- **Never:** auto-refresh deny lists at PAR with no pin (the very thing we
  just rejected — that is the uBO/Pi-hole default we're deliberately NOT
  copying).

---
## 4. Net mapping back to the plan

| question | answer |
| --- | --- |
| uBO/Pi-hole DBs | egress deny feed + trie-engine pattern + refresh model; **not** trust content, not dev allow |
| IPC on a box | UDS + JSON-RPC/ndjson, XDG_RUNTIME_DIR; HTTP only for genuinely remote (phone bridge) |
| open-source-ready | license per-artifact, proto/ contracts, provable verdict chain, lists-as-data, decoupled components |

The S0–S6 build order in `rust-security-core.md` is unchanged; this doc
just fixes the *interfaces* so the pieces we ship (and later publish) are
the ones we wanted all along.
