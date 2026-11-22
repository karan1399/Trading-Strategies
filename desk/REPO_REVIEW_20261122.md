# Repository Review — Trading-Strategies
**Date:** 2026-11-22 · **Commit:** `02d7ea79` · **Reviewer:** Arena agent
**Scope audited:** 2,029 tracked files · 1.6 GB working tree · 51M `.git` · 2,860 commits

---

## Verdict

**The code is in good health. The *operational envelope* around it is not.**

`tsc --noEmit` passes clean. `eslint .` passes clean. Build and serve work. No secrets are
tracked. The terminal itself (51 API routes, 55 lib modules, 232-asset universe) is sound.

Everything that needs fixing is in **repo hygiene, doc-drift, and the 83 % of commit history
that is machine noise** — the infrastructure that a small army of agents and daemons now shares.

Ranked by risk, not by effort.

---

## P0 — Do these first (risk of wrong agent behaviour)

### 1. README is factually wrong about the universe
| | Says | Reality |
| :--- | :--- | :--- |
| README line 13 | "**61-asset** universe (30 crypto · 28 TradFi · 3 classic)" | **232** in `curated.ts`, **231** live in the screener |

An agent reading the README will believe it evaluated the universe when it saw 61 names. This is
the exact failure mode from R825 — believing coverage was complete when it wasn't. **Fix the number,
and stop hardcoding it** — derive it from `curated.ts` in the build step so it cannot drift again.

### 2. Three competing copies of the canonical specification
```
desk/CANONICAL_MASTER_SPECIFICATION.md              ← the real one
desk/CANONICAL_MASTER_SPECIFICATION.v2.0.0-backup.md
desk/CANONICAL_MASTER_SPECIFICATION_V1.md
```
When an agent is told "obey the spec", which one? Different versions carry different gates
(the 1.0 % equity cap at L423 is only in v2). **Delete the V1 and the backup**, or move them to
`desk/archive/` and add a one-line header at the top of the survivor declaring it authoritative.

### 3. 100 hardcoded `/home/z/...` paths across 74 scripts
This is the bug class that already bit us: `near_miss_resolver.py` crashed **after** writing the
desk report because of one hardcoded operator path. I verified the **live daemon path is currently
clean** (`desk_round.sh`, `desk_cron_round.cjs`, `desk_daemon.mjs`, `near_miss_resolver.py`,
`dump_for_llm.py` all pass) — but 74 scripts will silently fail the moment they run off the
operator's machine.

Worst offenders: `scripts/report/*.js` (hardcodes `/home/z/my-project/download/...` for docx output),
`scripts/verify_runtime_environment.py` (hardcodes `C:\Users\SIGMA\...`).

**Fix:** replace every `/home/z/my-project` with `Path(__file__).resolve().parent.parent` and every
output dir with `os.environ.get("DESK_OUT", "download")`. ~2 hours with a sed + manual pass.

### 4. `scripts/verify_runtime_environment.py` verifies skills that are not installed
It checks for 8 skill paths under `C:\Users\SIGMA\.gemini\config\...` — including
`trading-skills/risk-reward-sanity-check`. Those live on the operator's Windows box, not here.
We just installed `skills/` **into the repo** with a different layout. So the verification script
reports on a filesystem the repo cannot see.

**Fix:** point it at `skills/` (repo-relative) so it actually verifies what this environment has.

---

## P1 — Structural debt

### 5. 83 % of commit history is daemon noise
```
total commits                2,860
"inbox/outbox/mt5/wake sync" 2,336   (81.7 %)
"auto-commit (pre-sync)"        43   ( 1.5 %)
                              ─────
                              2,379   (83.2 %)
```
On 2026-11-11 alone: **1,363 commits**. The real work is unfindable — `git log` is useless for
review, and `git bisect` is impractical. Every agent that tries to understand history pays this tax.

**Fix (low effort, high value):** the daemon should `git commit --amend` its own sync commit when
the previous commit is also a sync commit within N minutes, or squash to one sync commit per hour.
Nothing about correctness changes; `git log` becomes readable.

### 6. `desk/` is a drawer, not a desk
- **938 tracked files** in `desk/`
- 408 in `desk/inbox/`, 420 in `desk/outbox/` — every one a per-round JSON, kept forever
- `desk/log.md` is **19,777 lines / 1.4 MB** and is append-only
- 46 markdown files at `desk/*.md` top level, overlapping in authority
  (e.g. `TRADE_FORMULATION_MANUAL.md`, `LLM_TRADE_FORMULATION_PLAYBOOK.md`,
  `PLAYBOOK-EA-VALIDATED.md`, `SWARM_MASTER_PROMPT.md`, `PROTOCOL.md` — five documents that all
  claim to tell an agent what to do)

**Consequence:** an agent cannot determine precedence. This is the same failure as #2 at scale.

**Fix:**
1. Rotate `desk/log.md` yearly → `desk/log/2026-11.md`; keep a 200-line `log.md` index.
2. Archive `inbox/`+`outbox/` older than 30 days into `desk/archive/rounds/YYYY-MM/`.
3. Add `desk/AUTHORITY.md` — a 20-line document naming exactly which file wins on each topic
   (spec → X, playbook → Y, risk limits → Z). One page, kills the ambiguity.

### 7. 93 MB of `desk/` is committed telemetry, and `.gitignore` already forbids the rest
`.gitignore` says `desk/deepscan/` and `data/*` are local-only — correct policy. But the archive
still carries **~93 MB of JSON sweeps**:

| File | Size |
| :--- | ---: |
| `desk/archive/legacy_telemetry/crypto_full_telemetry.json` | 22 MB |
| `desk/archive/legacy_telemetry/crypto_sweep_r275.json` | 16 MB |
| `scripts/ops/deep_telemetry_360.json` | 14 MB |
| `desk/archive/legacy_telemetry/scratch_crypto_deep.json` | 13 MB |
| `desk/archive/legacy_telemetry/audit_full_universe.json` | 12 MB |

These are frozen snapshots of *past* market data — they will never be read again, and every clone
pays for them.

**Fix:** `git rm --cached` them, add `desk/archive/**/*.json` (over 1 MB) to `.gitignore`, keep a
`.sha256` manifest of what was removed so the evidence trail is intact.

### 8. 18 MB of reverse-engineered vendor JavaScript with 5 duplicate copies
`scripts/cg-js/` (18 MB) + `scripts/research/{liq-page,login-page,liq-new-ui}` hold **200 minified
Coinglass bundles**, including `_app-a28765d52f31b379.js` committed **five times** (2.1 MB each).

You reverse-engineered the Coinglass private API from these — genuinely valuable. But they are
*artifacts*, not source. And the repo publishes them publicly.

**Fix:** extract the ≤50 lines of useful endpoint/param knowledge into
`docs/cg-api-notes.md`, then delete the bundles. Keep the archive zip (`webmthly_202610.zip`)
outside the repo.

### 9. No CI, no tests for the terminal
- **0 unit tests** in `src/` — 51 API routes, 55 lib modules, zero tests
- `tests/` contains three *shell scripts*, not tests
- No `.github/workflows/`

`tsc` and `eslint` pass, which is a real signal — but nothing verifies that a route still *returns*
what it used to. R825's `liqlevels` coverage collapse and the `range=3d` hardcode would both have
been caught by a single contract test asserting "every `/api/cg/*` route returns `ok:true` for a
smoke symbol, and shelf levels include all three ranges".

**Fix — highest ROI in this whole review:** one GitHub Action running `tsc && eslint && npm run
build`, plus a `scripts/tests/test_api_contract.py` that walks all 51 routes against a local
server and asserts shape. ~150 lines, catches the entire class of "the terminal quietly changed".

---

## P2 — Cleanup

### 10. 22 of 67 declared dependencies are never imported
`@dnd-kit/*` (3), `@tanstack/react-query`, `@tanstack/react-table`, `framer-motion`,
`next-auth`, `next-intl`, `@mdxeditor/editor`, `@hookform/resolvers`, `@reactuses/core`,
`react-markdown`, `date-fns`, `prisma`… all declared, none imported in `src/`.

`prisma` is the telling one: the schema defines `User` and `Post` models — the untouched Next.js
starter template — and `src/lib/db.ts` is the only importer. **There is no database.** The whole
Prisma layer, `db:push`/`db:migrate`/`db:reset` scripts, and `DATABASE_URL` requirement serve
nothing.

**Fix:** `npm uninstall` the unused set, delete `prisma/` + `src/lib/db.ts` + the three `db:*`
scripts. Cuts install time and removes a false belief that there's persistence.

### 11. No Python dependency manifest for 385 `.py` files
No `requirements.txt`, no `pyproject.toml`. The scripts use `urllib`, `requests`, `pandas`,
`numpy`, plus MT5 bindings. Any fresh clone fails unpredictably.

**Fix:** `pip freeze > requirements.txt` from the host where the desk actually runs.

### 12. `node` version is not pinned
No `engines` field, no `.nvmrc`. Next 16 needs a specific major. Add both.

### 13. 189 of 628 scripts are never referenced anywhere
Almost all are the vendor JS from #8. After removing those the number drops sharply — do #8 first,
then re-measure before deleting anything real.

---

## What is genuinely good — keep doing this

- **`.gitignore` is unusually thoughtful.** The security comments on `desk/mt5.json` (live account
  login, balance, ticket levels — never push) are exactly right, and the awareness that "the repo is
  publicly cloneable" is correct threat modelling.
- **No secrets tracked.** I checked `.env` (untracked, correct), `.env.example` (tracked, correct),
  and scanned for `ghp_`/`sk-`/`AKIA`/PEM blocks — clean.
- **The API surface is orderly**: 51 route files with a consistent `{ok:true,...}` envelope and a
  shared `src/lib/cg/client.ts` that implements a real concurrency gate (6 slots), host-level 429
  cooldown, and a background-priority queue. That's better than most production code.
- **`near_miss_resolver.py` design** — schema-validated ledger, bucket threshold `n≥30`, explicit
  `INSUFFICIENT SAMPLE` verdict — is methodologically honest. The R824 portability fix we just
  landed makes it correct too.
- **`skills/INSTALLED.md`** — the gate-mapping table I added is the pattern the whole repo needs
  more of: state what a thing is, what it replaces, what it does not do.

---

## Suggested order of work

| # | Task | Effort | Risk removed |
| :-- | :--- | :--- | :--- |
| 1 | Fix README asset count (232, derived) | 15 min | agents mis-scoping universe |
| 2 | Archive V1 + backup spec; write `desk/AUTHORITY.md` | 45 min | agents following wrong spec |
| 3 | Contract test + CI (tsc/eslint/build + 51-route walk) | 3 h | silent API regressions |
| 4 | Remove unused deps + Prisma layer | 1 h | false persistence belief |
| 5 | `/home/z/` → repo-relative across 74 scripts | 2 h | 74 latent crash sites |
| 6 | Squash daemon sync commits | 2 h | unusable git history |
| 7 | `git rm --cached` the 93 MB telemetry + 18 MB vendor JS | 1 h | clone weight, public artifact leakage |
| 8 | Rotate `log.md`, archive old inbox/outbox | 1 h | agent readability |

**Total: about one working day.** Items 1–3 are the ones that actually change what an agent does
tomorrow; everything below that is debt service.

---

## One thing I could not verify

I did not run the 51-route contract walk during this review (it needs the terminal serving and
would take ~15 minutes with the rate limits documented in `DATA_COVERAGE_AUDIT.md`). The claim
"No route is silently broken today" is therefore **UNVERIFIED-BY-AGENT** — the R825 run showed
13 of 15 routes at 100 %, but `liqlevels`/`fp`/`cvd`/`market` were partially unavailable, and I have
not determined whether that is purely rate-limiting or a subset that is genuinely broken.
That test is item #3 above, and I'd like to write it.
