# Claude Code on Modal — setup

Run Claude Code in a GPU-enabled Modal sandbox with a web terminal, using your
Claude.ai OAuth (no API key needed).

## One-time setup

1. **Install and authenticate Modal**
   ```bash
   pip install modal
   python3 -m modal setup
   ```
   (If `modal …` gives `command not found`, pip put the entrypoint in a
   user-scripts dir that isn't on `$PATH`. Using `python3 -m modal …` avoids
   that — every command below uses this form.)

2. **Create the required Modal secrets**

   ```bash
   # Claude Code OAuth (pulled from your macOS Keychain)
   python3 -m modal secret create claude-code-secret \
     CLAUDE_CREDENTIALS="$(security find-generic-password -s 'Claude Code-credentials' -w)"

   # GHCR pull auth — required to pull the private dev-container image
   python3 -m modal secret create ghcr-secret \
     REGISTRY_USERNAME=ThatE10 REGISTRY_PASSWORD=$(gh auth token)
   ```

   Optional, only if you want to clone private GitHub repos into the sandbox:
   ```bash
   python3 -m modal secret create github-secret GH_TOKEN=$(gh auth token)
   ```

## Launching

### Interactive launcher (recommended)

```bash
python launch.py            # menu — pick a saved preset or create one
python launch.py --list     # print presets, most-used first
python launch.py --volumes  # print storage volumes and which presets use them
python launch.py --forget N # delete preset #N
python launch.py --presets path/to/catalog.json   # merge in a project catalog
```

Your own presets are stored at `~/.config/claude-code-modal/presets.json` and
sorted by usage count, so your most-used config is always at the top of the
menu. Each preset stores: name, base image, GPU, CPU, RAM, repo, ref, session
hours, whether to attach a fresh `gh auth token` on launch, and its
persistent-storage volume name.

Tokens are never written to the preset file — the `gh auth token` value is
resolved at launch time.

### Preset catalogs

Presets are merged from three places, each overriding the last by preset name:

| Source   | Where                                              |
| -------- | -------------------------------------------------- |
| built-in | `presets.default.json` next to `launch.py`         |
| project  | `--presets PATH` (repeatable) or `$DEVCONTAINER_PRESETS` (`:`-separated) |
| user     | `~/.config/claude-code-modal/presets.json`         |

The built-ins (`cpu-small`, `gpu-l40s`, `gpu-h100`, …) give a fresh machine
something to launch before anyone has defined a preset. A project catalog lets
a repo version the hardware profiles its work actually needs, so they travel
with the code and everyone — including an agent working in the repo — sees the
same set. Your own file always wins.

`--list` tags each entry with the catalog it came from. Launching a catalog
preset **adopts** it: a copy lands in your `presets.json`, which is where its
usage count lives and where you can retune it. Until then the catalog stays the
single source of truth, so `launch.py` never silently forks a shared preset.

### Persistent storage

Presets are persistent by default. Each is backed by a named set of Modal
Volumes — `ccm-<volume>-workspace`, `ccm-<volume>-local` and
`ccm-<volume>-claude` — mounted at `/root/workspace`, `/root/.local` and
`/root/.claude`, plus the HuggingFace cache volume (`claude-code-hf-cache` by
default), which is *shared* across sandboxes rather than per-preset since it's
read-mostly after the first download. The volume name defaults to the preset
name, but presets and volumes are decoupled: creating a preset opens a volume
picker listing the volumes your other presets use — pick one to share storage
between presets, type a new name, or choose the ephemeral option for a
throwaway box.

At launch time, append `v` to your menu choice (e.g. `1v`) to run a preset on
a different volume for just that session, or save the change back to the
preset when prompted. `python launch.py --volumes` lists every volume and
which presets use it. Don't run two sandboxes on the same volume at once —
concurrent writers race.

Relaunch a preset and its files, editor extensions, shell history, model
weights, and Claude Code state are all restored. With `claude_code_modal.py`
directly, pick the volume set with `--volume-name NAME` (persistent by
default; pass `--no-persist` for an ephemeral box).

### Direct invocation

```bash
# Defaults: dev-container image, no GPU, Modal-default CPU/RAM
python claude_code_modal.py

# With a repo, GPU, and larger resources
python claude_code_modal.py \
  --repo owner/repo --ref main \
  --gpu A100-80GB --cpu 8 --memory 32768 --hours 8

# Fast, cheap path — skip the 20GB dev-container image
python claude_code_modal.py --image slim
```

## Connecting

The launcher prints all the ways in when the sandbox comes up:

```
SSH          : ssh root@<host> -p <port>
Web terminal : https://....modal.host   (bash shell, type `claude`)
VS Code      : https://....modal.host
Marimo       : https://....modal.host
Shell access : modal shell <sandbox-id>  (fallback if SSH won't connect)
```

**SSH is the recommended way in** — it's a real TCP tunnel (`unencrypted_ports`
in Modal terms — no TLS wrapping, so any normal `ssh` client just works),
carries a proper terminal, and survives disconnects/reconnects better than the
ttyd web terminal or `modal shell`, which stay available as fallbacks.

Auth: the launcher auto-detects `~/.ssh/id_ed25519.pub` / `~/.ssh/id_rsa.pub`
on your machine and installs it as an authorized key for `root`. Override with
`--ssh-public-key <text-or-path>`, add `--ssh-password <pw>` for password auth
too, or `--no-ssh` to disable sshd entirely (falls back to ttyd as the
keep-alive process). If neither a key nor a password resolves, sshd still
starts but nothing can log in — the launcher warns you when that happens.

## Persistent storage

By default each sandbox mounts four [Modal
Volumes](https://modal.com/docs/guide/volumes): `/root/workspace` (your repo
clone + scratch space), `/root/.claude` (Claude Code config/history —
including the settings-repo checkout), `/root/.local` (code-server extensions,
`pip install --user` packages), and a shared HF weights cache. Modal
background-commits volume writes every few seconds and on shutdown, so nothing
special is needed to save your work — just don't `rm` the volume.

Volumes are named `ccm-<slug>-{workspace,claude,local}`, where `<slug>` comes
from `--volume-name` (the interactive launcher passes your **preset name**
automatically), falling back to `--repo` or `default`. Relaunching the same
preset/`--volume-name` reattaches the same disks — repo clone, shell state,
and Claude session all resume where you left off, even after a crash or a
`--hours` timeout.

Pass `--no-persist` for a fully ephemeral sandbox (nothing survives a
restart) — useful for quick, throwaway runs.

### Running multiple agents at once

Each preset/`--volume-name` maps to its own volume set, so running **different
presets concurrently is safe** — separate sandboxes, separate disks, no shared
state. Don't launch two sandboxes with the *same* `--volume-name` at the same
time, though: Modal allows concurrent mounts, but writes from two live
containers to the same volume aren't reconciled and can clobber each other.

If you want several Claude agents collaborating on the *same* checkout inside
one sandbox, isolate them with `git worktree add` under `/root/workspace/code`
rather than pointing multiple agents at one working tree — that's a git
feature, not something this launcher wires up automatically.

## Secrets attached to the sandbox

Beyond `claude-code-secret` (always) and `ghcr-secret` (for the image pull),
the launcher attaches these Modal secrets by default so the sandbox has your
usual API keys:

- `huggingface-secret` (`HF_TOKEN`) → written to `~/.cache/huggingface/token`
- `github-secret` (`GH_TOKEN` or `GITHUB_TOKEN`) → `gh auth login` runs at startup
- `wandb-secret` (`WANDB_API_KEY`) → enables online W&B logging instead of the
  file-only/offline fallback

Add more via `--secret NAME` (repeatable) on `claude_code_modal.py`, or when
creating a preset the launcher prompts for a comma-separated list. Common
extras: `openai-secret`, `anthropic-secret`.

On startup the sandbox runs the dev-container's `import-secrets.sh`, which
writes each token to its canonical config location (HF, WandB, Modal, gh).
Pass `--secret ''` once to disable the default secrets entirely.

## Base images

| `--image` value                         | When to use                                          |
|-----------------------------------------|------------------------------------------------------|
| `ghcr.io/thate10/dev-container:latest`  | Default. vLLM + torch + CUDA, plus Claude Code + ttyd. |
| `slim`                                  | Debian slim. Fast pull, CPU only, no torch/CUDA.     |

Any other registry tag also works via `--image <tag> --registry-secret <name>`.

## GPU options

Modal-supported strings: `T4`, `L4`, `A10G`, `L40S`, `A100-40GB`, `A100-80GB`,
`H100`, `H200`. Append `:N` for multi-GPU (e.g. `H100:2`).

Omit `--gpu` for CPU-only sandboxes.

## Troubleshooting

**`Image build … terminated due to external shut-down. Please try again.`**
Transient Modal builder error, typically on the first pull of the ~20GB
dev-container image. The builder's tmp directory can get wiped mid-pull.
Just re-run — subsequent pulls hit Modal's layer cache and complete fast.

**`command not found: modal`** — pip installed the entrypoint outside `$PATH`.
Use `python3 -m modal` instead (all commands in this doc use that form).

**Image pull auth failure** — regenerate `ghcr-secret` with a fresh
`gh auth token`; PATs expire.
