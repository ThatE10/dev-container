# Developer Log

## 2026-07-21 — Global ~/.claude settings sync + Modal launcher parity

**Goal.** (1) Load & keep-updated global Claude Code settings in `~/.claude` from
a git repo. (2) Bring the Modal launcher (`claude_code_modal.py`) up to parity
with the Docker container — expose Marimo and browser VS Code, not just a
terminal.

**Settings-repo model.** Inspected `ThatE10/claude-code-settings`: it tracks
`agents/ hooks/ skills/ memory/ .claude-tools/ tests/` and its `.gitignore`
excludes runtime files (`settings.json`, `sessions/`, `projects/`,
`history.jsonl`, credentials). So the repo is *designed to be* `~/.claude`.
Chosen approach: make `~/.claude` a checkout of the repo and `git pull` each
start. Because `~/.claude` already exists (credentials written by the entrypoint;
it's also the `claude-data` volume), a plain `git clone` won't work (needs an
empty dir) — so `scripts/sync-claude-settings.sh` clones `--no-checkout` into a
temp dir, moves `.git` into `~/.claude`, then `checkout -f`. `-f` only overwrites
*tracked* files; untracked credentials/sessions are left alone. All steps are
best-effort (missing repo / offline / diverged branch → warn + exit 0) so the
container always starts. Knobs: `CLAUDE_SETTINGS_REPO` (owner/repo or URL, blank
= off), `CLAUDE_SETTINGS_BRANCH`, `CLAUDE_SETTINGS_SYNC`. Private repos use
`GITHUB_TOKEN`. Wired into `entrypoint.sh` right after the credentials block;
added to compose env, `.env.example`, and the CI `bash -n` lint list.

> NOTE: the settings repo's `.gitignore` currently has committed merge-conflict
> markers (`<<<<<<< / ======= / >>>>>>>`). Functionally OK (both ignore blocks
> still apply) but should be cleaned up in that repo — out of scope here.

**Modal launcher (`claude_code_modal.py`).** Added code-server (installed under
`/usr/local`, same as the Docker image) and `marimo` (pip) to the Modal image.
`build_startup()` now restores credentials, optionally syncs the settings repo
(same clone→move-.git→checkout dance, inlined), backgrounds code-server + Marimo,
and keeps ttyd in the foreground. Exposes an encrypted tunnel per surface
(7681 terminal / 8443 VS Code / 2719 Marimo) and prints all URLs. New flags:
`--no-code-server`, `--no-marimo`, `--claude-settings-repo`,
`--claude-settings-branch`. `--repo` clone behaviour unchanged. (The user refers
to this file as "launch.py"; it is `claude_code_modal.py` — no rename yet.)

**CI impact.** Same as before: smoke-test builds only the smoke stage and now
also lints `sync-claude-settings.sh`. code-server/settings changes land in the
`dev` stage, exercised by the full build on the PR. `claude_code_modal.py` is not
part of any image and isn't CI-tested (validated locally with `py_compile`).

---

## 2026-07-21 — Browser VS Code, persistent context, easy up/down

**Goal.** Make this a container you can spin up, code in, save, and relaunch
later with everything intact — a persistent personal dev box, not a throwaway.

**Changes.**

1. **code-server (browser VS Code).** Installed the standalone binary in the
   `dev` Dockerfile stage (`ARG CODE_SERVER_VERSION`, empty = latest; pinnable
   via `--build-arg`). Exposed port **8443**. The entrypoint writes
   `/root/.config/code-server/config.yaml` at start and launches code-server in
   the background pointed at `/root/workspace`. Auth: uses `CODE_SERVER_PASSWORD`
   if set, otherwise `auth: none` (same trusted-network/SSH-tunnel model already
   used for JupyterLab). Toggle with `START_CODE_SERVER=0`.

2. **Persistent context across rebuilds.** Added two named volumes in
   `docker-compose.yml`:
   - `dev-local → /root/.local` — captures code-server extensions/settings,
     `pip install --user` packages, `~/.local/bin`, and shell history in one
     volume.
   - `claude-data → /root/.claude` — Claude Code config + chat history.

   Chose `/root/.local` deliberately: it consolidates editor state + user pip
   installs + user bins without shadowing any image-baked config (code-server
   installs to `/usr/lib`, not `~/.local`). Did **not** volume-mount
   site-packages or `/root` wholesale — that would shadow the base image's
   torch/vLLM and hit the named-volume stale-config trap on rebuild. Runtime
   `pip install --user` / project `.venv`s under `./workspace` are the
   documented way to persist deps.

   Entrypoint now also persists shell history to the `dev-local` volume
   (`HISTFILE`, `histappend`, per-command flush) and puts `~/.local/bin` on PATH.

3. **Easy up/down.** Added a `Makefile` (`up`/`down`/`shell`/`logs`/`rebuild`/
   `status`/`new NAME=…`/`clean-context`) and a `README.md` documenting the
   spin-up → code → save → relaunch workflow and exactly what persists vs. not.
   Added `CODE_SERVER_PASSWORD` to `.env.example`.

**CI impact.** The smoke-test job builds only the `smoke-test` stage (no
code-server; it only lints the scripts with `bash -n`) and stayed green. The
`build-and-push` job builds the full `dev` stage on PRs too (it just doesn't
push unless it's a merge to `main`), so it *does* exercise the code-server
install on this PR.

**Follow-up fix (CI run 29826481698).** First push failed the full build at the
code-server step, exit 127. Root cause: `--method standalone` installs into
`~/.local/bin`, which isn't on `PATH` in the build `RUN`, so the `&&
code-server --version` verification couldn't find the binary. Worse, `~/.local`
is exactly where the `dev-local` volume mounts — the binary would have been
shadowed by the volume and pinned to a stale version on rebuild. Fixed by
installing with `--prefix /usr/local` (binary → `/usr/local/bin/code-server`,
baked in the image, outside the volume) and verifying via the full path. Only
editor user-data under `~/.local/share/code-server` now lives in the volume.

**Kept GPU-required** per request — base image and GPU reservation unchanged.

---

## 2026-07-09 — CI build failure investigation (run 28955705075)

**Context.** GitHub Actions run for commit `f9fb648` (`Fix workflow: lowercase
ghcr.io image tag, pin action versions`) failed on the `Build full image & push
to ghcr.io` job. Full-image build takes ~15–20 min per attempt, so this log
captures root-cause analysis before the next push.

Two separate defects surfaced, stacked in one failure. Fixing only the visible
one (ipykernel) will unmask the second one on the next run.

---

### Issue 1 — Critical packages get uninstalled and reinstalled during `pip install -r requirements.txt`

**What we see in the log.** During the `RUN pip install --no-cache-dir -r
/tmp/requirements.txt` step (Dockerfile stage `dev`), pip uninstalls and
replaces packages that ship with the `vllm/vllm-openai:v0.6.4` base image:

| Package         | Base image    | Reinstalled as | Notes                                    |
| --------------- | ------------- | -------------- | ---------------------------------------- |
| torch           | 2.5.1         | 2.13.0         | Major bump — breaks compiled CUDA kernels |
| transformers    | 4.46.2        | 5.13.0         | Major bump                                |
| datasets        | 3.1.0         | 5.0.0          |                                           |
| accelerate      | 1.1.1         | 1.14.0         |                                           |
| huggingface_hub | 0.26.2        | 1.22.0         | Major bump                                |
| tokenizers      | 0.20.3        | 0.22.2         |                                           |
| safetensors     | 0.4.5         | 0.8.0          |                                           |
| numpy           | 1.26.4        | 2.5.1          | NumPy 2 ABI break                         |
| pyarrow         | 18.0.0        | 24.0.0         |                                           |
| triton          | 3.1.0         | 3.7.1          |                                           |
| pyzmq           | 26.2.0        | 27.1.0         |                                           |
| msgspec         | 0.18.6        | 0.21.1         |                                           |

Additionally the whole CUDA 13 userspace stack was pulled in as a new
dependency (`cuda-toolkit-13.0.3.0`, `nvidia-cublas-13.1.1.3`,
`nvidia-cudnn-cu13-9.20.0.48`, `nvidia-nccl-cu13-2.29.7`, …). The vLLM base
image is built against CUDA 12.x drivers/runtime, so a CUDA 13 stack living
next to it is at best wasted disk and at worst a runtime conflict.

**Root cause.** `requirements.txt` lists `sae-lens`, `peft`, `trl` unpinned.
As of 2026-01, pip resolves these to their latest releases
(`sae-lens 6.45.3`, `peft 0.19.1`, `trl 1.7.1`), and their transitive
constraints — combined across all three — force pip to upgrade `torch`,
`transformers`, `numpy`, etc.

Concrete triggers from the log:

- `sae-lens 6.45.3` → `Collecting transformers<6.0.0,>=4.38.1` → resolver
  chose `transformers-5.13.0` (base was 4.46.2, which satisfies the range,
  but a co-installed dep — likely `trl 1.7.1` or `transformer-lens 3.5.1` —
  required `>=5`).
- `peft 0.19.1` → `Collecting torch>=1.13.0` → resolver chose `torch-2.13.0`.
- `seaborn` → `Collecting numpy!=1.24.0,>=1.20` → resolver chose `numpy-2.5.1`.

Pip's default upgrade strategy (`only-if-needed`) IS active — the upgrades
happen because at least one dep in the resolved set genuinely required newer
than what the base image ships. The Dockerfile comment says "Do NOT list
torch / transformers / … here — re-listing risks pip downgrading them."
That guidance is insufficient: transitive deps of unpinned
`sae-lens`/`peft`/`trl` cause the upgrade too, whether or not torch is
listed.

**Fix (in priority order).**

1. **Add `constraints.txt`** pinning the vLLM v0.6.4 baseline for the packages
   the base image ships. Reference it from the Dockerfile:

   ```
   RUN pip install --no-cache-dir -c /tmp/constraints.txt -r /tmp/requirements.txt
   ```

   Contents (verified from the failure log):

   ```
   torch==2.5.1
   transformers==4.46.2
   huggingface_hub==0.26.2
   tokenizers==0.20.3
   datasets==3.1.0
   accelerate==1.1.1
   safetensors==0.4.5
   numpy<2
   pyarrow==18.0.0
   triton==3.1.0
   ```

   Constraints are the correct tool here because pip *fails loudly* (rather
   than silently swapping the image out from under you) if a requested package
   is genuinely incompatible with the pin.

2. **Pin `sae-lens`, `peft`, `trl`** to versions released against
   torch 2.5 / transformers 4.46 (i.e. late-2024 releases matching the vLLM
   base image cut). The exact versions need one round of resolution against
   the constraints file; expected shape:

   - `sae-lens>=5,<6`  (6.x requires transformers 5)
   - `peft==0.13.*`
   - `trl==0.12.*`

3. **Independently:** `seaborn` pulls `matplotlib` and 60MB of plotting deps
   into a container that already includes `plotly`. If seaborn isn't needed
   in-image, drop it — it also is the reason numpy 2 got picked up.

**Expected next-failure preview if we push *only* the ipykernel fix.** The
`Test full image imports (no GPU)` step (`docker run … python -c "import
torch, transformers, vllm, …"`) will fail. vLLM v0.6.4 ships pre-compiled
FlashAttention/PagedAttention ops linked against torch 2.5.1's C++ ABI; those
ops will refuse to load against torch 2.13.0. Even if that hurdle passed,
`sae-lens 6.45.3` importing `transformers 5.13` may then trip API changes
between transformers 4→5. Do not push the ipykernel fix without the
constraints file.

---

### Issue 2 — `python -m ipykernel install` exits 127

**What we see in the log.**

```
#13 [dev 6/10] RUN python -m ipykernel install \
        --name "dev-container" \
        --display-name "Dev Container (CUDA $(nvcc --version 2>/dev/null | grep release | awk '{print $5}' | tr -d ,))"
#13 ERROR: process "…" did not complete successfully: exit code: 127
```

Exit 127 means "command not found". Two contributing factors:

1. **`python` is not on PATH inside the vLLM base image.** The
   `vllm/vllm-openai` image installs Python as `python3` only — there is no
   `python → python3` symlink. `RUN python -m …` therefore fails before
   ipykernel is ever invoked.
2. The `$(nvcc --version …)` subshell in the `--display-name` runs at
   build time. `nvcc` may not be on PATH during the buildkit RUN layer;
   `2>/dev/null` swallows the error but the resulting display name is
   `"Dev Container (CUDA )"` — cosmetically broken and pointless.

**Fix.** Already committed locally as `24c3641` (unpushed at time of writing):

```
RUN python3 -m ipykernel install \
        --name "dev-container" \
        --display-name "Dev Container (GPU)"
```

This will resolve exit-127 — but see Issue 1: don't push this alone.

---

### Other things noticed (not blocking, but worth recording)

- The build log shows
  `#8 ERROR: failed to configure registry cache importer: ghcr.io/thate10/dev-container:buildcache: not found`.
  This is the first-run cold-cache case for the registry buildcache — the
  package doesn't exist yet because we've never successfully pushed. It is
  **not** a failure and buildkit continues past it. Ignore on the next run;
  it'll disappear after the first successful push writes the buildcache.
- Deprecation warning: `actions/checkout@v4.2.2`, `docker/build-push-action@v6`,
  `docker/login-action@v3.4.0`, `docker/setup-buildx-action@v3.10.0` all
  target Node 20 which GitHub is deprecating. Bump when convenient; not
  blocking.

---

### Recommended remediation order (single 15-min CI cycle)

1. Add `constraints.txt` (Issue 1 fix #1).
2. Pin `sae-lens`, `peft`, `trl` in `requirements.txt` to base-image-era
   versions (Issue 1 fix #2).
3. Push together with the already-committed `24c3641` (Issue 2 fix).
4. Watch the `Test full image imports` step — it should now pass because
   torch/transformers stay at 2.5.1/4.46.2 and vLLM's compiled kernels load
   cleanly.

If step 4 still fails, the next likely culprit is an API break inside
`sae-lens` / `peft` / `trl` against transformers 4.46 — resolvable by picking
a slightly older release of the offending package.
