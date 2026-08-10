# Dev Container

A GPU dev container you can **spin up, code in from your browser, save, and
relaunch later with everything intact** — your project files, editor extensions,
installed packages, shell history, and Claude Code setup.

Built on `vllm/vllm-openai` (PyTorch + CUDA kernels prebuilt) with **VS Code in
the browser (code-server)**, JupyterLab, Marimo, vLLM, and the Claude Code CLI.

---

## Requirements

- Docker + Docker Compose v2
- An **NVIDIA GPU** with the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
  (the container reserves a GPU and won't start without one)

## Quick start

```bash
cp .env.example .env      # then fill in the values you need
make up                   # build/start in the background
```

Open **http://localhost:8443** → full VS Code in your browser, already pointed at
your workspace.

Stop when you're done:

```bash
make down                 # container is removed; your context is kept
```

Come back later with `make up` — your project, extensions, and history are all
still there.

Run `make help` to see every command (`up`, `down`, `shell`, `logs`, `rebuild`,
`new`, `status`, …).

## Start a new project

```bash
make new NAME=my-project
```

Creates `workspace/my-project/` with a git repo and a `.venv`. Open it in VS Code
at http://localhost:8443 and start coding. Install project deps into the venv:

```bash
cd /root/workspace/my-project
source .venv/bin/activate
pip install <whatever>
```

Because `./workspace` is a **host bind mount**, everything under it — including
each project's `.venv` — lives on your machine and persists automatically.

## What persists (and what doesn't)

Everything you care about survives `make down` **and** a full `make rebuild`:

| Persisted                                   | Where it's stored            |
| ------------------------------------------- | ---------------------------- |
| Project code + per-project `.venv`s         | `./workspace` (host bind)    |
| VS Code extensions & settings               | `dev-local` volume           |
| `pip install --user` packages, `~/.local/bin` | `dev-local` volume         |
| Shell history                               | `dev-local` volume           |
| Claude Code config & chat history           | `claude-data` volume         |
| Downloaded model weights (HuggingFace)      | `hf-cache` volume            |

**Not** persisted (rebuilt from the image each time): system packages installed
at runtime with `apt-get`, and any files written outside the paths above. To make
a system-level change stick, add it to the `Dockerfile` and `make rebuild`.

> **Tip:** install Python packages you want to keep with `pip install --user`
> (goes to the persisted `dev-local` volume) or into a project `.venv` under
> `./workspace`. A bare `pip install` writes into the image layer and is lost on
> rebuild.

## Ports

| Port | Service                              | URL / usage                         |
| ---- | ------------------------------------ | ----------------------------------- |
| 8443 | code-server (browser VS Code)        | http://localhost:8443               |
| 2222 | SSH                                  | `ssh root@localhost -p 2222`        |
| 8888 | JupyterLab                           | run `jupyter lab` in the container  |
| 2719 | Marimo                               | run `marimo edit` in the container  |
| 8080 | vLLM OpenAI-compatible API / general | run the vLLM server in the container|

code-server auto-starts; the others you launch from a terminal (in VS Code or
over SSH) when you need them.

## Security note

By default the browser VS Code runs with **auth disabled** and binds to all
interfaces, matching the JupyterLab setup — fine on `localhost` or behind an SSH
tunnel. If the host is reachable by others, set a password in `.env`:

```bash
CODE_SERVER_PASSWORD=some-strong-password
```

Or tunnel instead of exposing the port: `ssh -N -L 8443:localhost:8443 root@host -p 2222`.

## Global Claude Code settings

On start, the container syncs a settings repo into `~/.claude` so your Claude
Code **agents, hooks, skills, and memory are versioned and auto-updated** — the
same setup follows you into every container. On first run it checks out the repo
into `~/.claude`; on every start after that it `git pull`s the latest. Your
credentials and session state are gitignored by that repo and never touched.

Configure it in `.env` (defaults shown):

```bash
CLAUDE_SETTINGS_REPO=ThatE10/claude-code-settings   # owner/repo or URL; blank = off
CLAUDE_SETTINGS_BRANCH=main
CLAUDE_SETTINGS_SYNC=1                               # 0 disables syncing
```

Private settings repos work too — the sync uses your `GITHUB_TOKEN` when the
repo is given as `owner/repo`.

## Run on Modal (no local GPU)

Launch the same surfaces — SSH, browser VS Code, Marimo, and a web terminal
with `claude` pre-authenticated — on [Modal](https://modal.com) instead of your
own hardware. Each gets its own tunnel URL, and (unlike a bare Modal sandbox)
the workspace, `~/.claude`, and `~/.local` persist on Modal Volumes across
restarts, so it behaves like a real preconfigured GPU VM you can stop and
resume. One-time setup is in [`SETUP.md`](SETUP.md).

The easiest way in is the interactive launcher, which saves presets
(GPU/CPU/RAM/repo + VS Code / Marimo / settings-repo) sorted by how often you
use them:

```bash
python launch.py            # pick or create a preset
python launch.py --list     # show saved presets
python launch.py --volumes  # show storage volumes and which presets share them
```

Or call the underlying script directly:

```bash
python claude_code_modal.py --repo owner/repo --gpu A100-80GB
python claude_code_modal.py --image slim --no-marimo      # fast CPU box, VS Code only
```

### Batch jobs (run-then-die)

A dev box you leave running bills for the GPU whether or not it's computing.
When the unit of work is a *job* rather than a session, `--run` executes a
command in the checkout instead of parking on a shell, and `--exit-on-finish`
tears the sandbox down the moment it's done — commission, run, die:

```bash
python claude_code_modal.py \
  --repo owner/repo --ref 1a2b3c4 --gpu L40S \
  --run 'python train.py --config configs/big.yaml' \
  --exit-on-finish
```

The launcher blocks until the job ends and exits with the command's status, so
it composes with shell `&&` and CI. Notes:

- `--exit-on-finish` turns off VS Code, Marimo and SSH — a sandbox that dies
  with the job has no use for them, and there'd be no tunnels to print. Pass
  `--run` *without* it to run the command and then keep the box up for
  inspection, services and all.
- `--detach` returns as soon as the job is launched instead of waiting.
- Output is teed to `<workspace>/.last_run.log`; `modal container logs <id>`
  works too.
- `--ref` accepts a **commit SHA** as well as a branch or tag, which is what a
  reproducible run wants.
- `--post-clone scripts/setup.sh` runs a repo-provided script after the
  checkout and before the command — for linking a venv, wiring up a mount, or
  any other project-specific preparation.
- `--mount NAME:/path` mounts an extra Modal Volume, e.g. a results volume
  shared by every job. Use a path *outside* `/root/workspace` — volumes are not
  mounted inside one another.

Parallel jobs must not share a workspace volume (concurrent writers race).
Give each `--volume-name` of its own, or `--no-persist` and keep only the
shared results mount.

### Agents that launch their own containers

An agent working inside a sandbox often needs to commission *more* sandboxes —
to run a GPU job, fan a sweep out, or rebuild something on bigger hardware. It
can't, unless it has this launcher's source and Modal credentials.
`--with-launcher` gives it both:

```bash
python claude_code_modal.py --repo ThatE10/rspd --with-launcher
```

That clones `ThatE10/dev-container` to `<workspace>/dev-container`, attaches
the Modal secret (`--modal-secret`, default `modal-secret`, holding
`MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`), and exports `$DEV_CONTAINER_DIR` —
into the shell *and* `~/.bashrc`, since the consumer is usually a process
started long after launch. `import-secrets.sh` turns those tokens into
`~/.modal.toml`, so `modal` is authenticated with no further setup.

The checkout lands *beside* the main one rather than inside it, which is what
a sibling tool expects: RSPD's `modal_experiment.py` looks for the launcher at
`<its repo>/../dev-container`, and the primary checkout is `<workspace>/code`,
so the paths line up with nothing to configure.

`--extra-repo owner/repo[@ref]` is the general form (repeatable) for any other
side-by-side checkout. Both accept a pinned ref.

**Create the secret first**, or the launch fails:

```bash
modal secret create modal-secret \
    MODAL_TOKEN_ID=ak-... MODAL_TOKEN_SECRET=as-...
```

A Modal *Secret* is a named, server-side object, and it is **not** the same as
the local CLI credentials `modal token set` writes — having run `modal token
set` does not create it. That is the easy thing to conflate, so the launcher
catches the missing-secret error and says so rather than surfacing an SDK
traceback. Pass `--modal-secret ''` to skip the secret if credentials reach the
sandbox some other way.

Credentials that let a sandbox create more sandboxes are worth scoping
deliberately — an agent holding them can spend money on GPUs. Prefer a token
with the access you actually want the agent to have, and rotate it if it ever
lands somewhere it shouldn't.

### Persistent storage on Modal

Presets launched through `launch.py` are **persistent by default** — the same
context that survives `make down` locally now survives a Modal sandbox
shutdown too. Each preset is backed by a named set of Modal Volumes mirroring
the Docker volumes above:

| Persisted                                     | Mounted at              |
| --------------------------------------------- | ----------------------- |
| Project code + per-project `.venv`s           | `/root/workspace`       |
| code-server extensions/settings, shell history, `pip install --user` | `/root/.local` |
| Claude Code config & chat history             | `/root/.claude`         |
| Downloaded model weights (HuggingFace)        | `/root/.cache/huggingface` |

The volume name defaults to the preset name, so relaunching the same preset
restores its state — but presets and volumes are decoupled. Creating a preset
opens a volume picker listing every volume your presets already use, so two
presets can share one set of storage volumes; append `v` to a menu choice
(e.g. `1v`) to relaunch a preset on a different volume — one-off, or saved
back to the preset. Modal commits these volumes in the background and on
shutdown, so a session that hits its timeout still keeps its work. (Don't run
two sandboxes on the same volume at once — concurrent writers race.)

With `claude_code_modal.py` directly, storage is persistent by default (keyed by
`--volume-name`, falling back to the repo name); pass `--no-persist` for a
throwaway box:

```bash
python claude_code_modal.py --volume-name my-box --gpu A100-80GB   # resume with the same name
python claude_code_modal.py --no-persist --image slim             # ephemeral
```

## Secrets

`make up` reads `.env`. Populate only what you use — SSH auth, `HF_TOKEN`,
`ANTHROPIC_API_KEY`, `CLAUDE_CREDENTIALS`, `CLAUDE_SETTINGS_REPO`, Modal, W&B,
GitHub. See [`.env.example`](.env.example) for the full list and where to get
each value.

## Reset your context

To wipe the persisted editor/tool/Claude context (but keep your `./workspace`
code):

```bash
make clean-context
```
