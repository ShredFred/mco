<p align="center">
  <img src="https://raw.githubusercontent.com/mco-org/mco/main/docs/assets/brand/mco-cover-starry.jpg" alt="MCO — ten agent paths converging through a monumental M beneath a starry sky" width="100%" />
</p>

<h1 align="center">MCO</h1>

<p align="center"><strong>Orchestrate AI coding agents. Compare perspectives. Act with confidence.</strong></p>

<p align="center">
  <a href="https://www.npmjs.com/package/@tt-a1i/mco"><img src="https://img.shields.io/npm/v/@tt-a1i/mco?style=flat-square&color=cb3837&logo=npm&logoColor=white" alt="npm version" /></a>
  <a href="https://www.npmjs.com/package/@tt-a1i/mco"><img src="https://img.shields.io/npm/dm/@tt-a1i/mco?style=flat-square&color=cb3837" alt="npm downloads" /></a>
  <a href="https://github.com/mco-org/mco/stargazers"><img src="https://img.shields.io/github/stars/mco-org/mco?style=flat-square&color=f59e0b" alt="GitHub stars" /></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-22c55e?style=flat-square" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+" />
</p>

<p align="center">English · <a href="./README.zh-CN.md">简体中文</a></p>

MCO is a lightweight, CLI-first orchestration layer for AI coding agents. Give one task to the agents and models you choose, run them in parallel, and compare their raw answers before you act.

Use MCO for code review, implementation, architecture analysis, CI checks, and any workflow where one model's blind spots matter.

It works from a terminal or from another coding agent such as Claude Code, Codex, Cursor, Copilot, Pi, or OpenClaw.

> MCO is actively maintained. For a browser workbench with persistent agent identity and a shared task graph, see [Hive](https://hivehq.dev).

## Quick start

Install the CLI and its bundled `mco-cli` Skill:

```bash
npx @tt-a1i/mco@latest install
```

Check the agents available on your machine:

```bash
mco doctor --json
```

Run a read-only multi-agent review:

```bash
mco review \
  --repo . \
  --prompt "Review this repository for high-risk bugs." \
  --providers claude,codex,pi
```

Run a coding task with workspace write access:

```bash
mco run \
  --repo . \
  --prompt "Implement the requested change and run the relevant tests." \
  --providers codex,pi \
  --execution-mode write
```

MCO never infers a provider/model team from detected binaries. If neither `--providers` nor `--agent` is supplied, a top-level `providers` config entry is used as the saved default; without one, the team must be selected explicitly. A calling agent should still show and confirm the resolved provider/model team with the user before dispatch.

## Delegation backend in this fork

[ShredFred/mco](https://github.com/ShredFred/mco) adds a durable session submission path for an outer harness. The harness keeps product coordination, GitHub issue ownership, provider selection, and worktree provisioning. MCO executes requests and returns receipts; it does not create tickets, branches, or worktrees.

These fork changes are installed from this checkout, **not** from the upstream npm package above. Use Python 3.10+ (for example 3.11):

```bash
git clone https://github.com/ShredFred/mco.git
cd mco
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[memory]'
mco --version
```

Create a session in an externally managed worktree and submit an implementation request:

```bash
mco session ensure --repo /path/to/worktree --provider codex --name implementation
mco session send implementation "Implement the selected ticket and run its tests" \
  --repo /path/to/worktree --execution-mode write \
  --request-key ticket-42-implementation-v1 --ticket-ref 'ShredFred/mco#42' \
  --no-wait --json
mco session result implementation 1 --repo /path/to/worktree --json
mco session cancel implementation --request-id 1 --repo /path/to/worktree --json
```

Replace `1` with the returned `request_id` and the example ticket reference with your real ticket. Session turns default to `read_only`; implementation requires explicit `write` or `yolo` permissions. A ticket reference is opaque metadata and does not cause GitHub API calls.

- Acceptance is committed to `.mco/sessions/<name>/requests.sqlite3` before ACK. Repeating a request key with the same prompt, permissions, and ticket reference returns the same request; changing its payload is a conflict. Reusing a key does **not** retry execution.
- Result receipts distinguish `queued`, `running`, `succeeded`, `failed`, `cancelled`, and `unknown` through `lifecycle`; queued/running queries return `status: pending`. Terminal results remain queryable after daemon shutdown. No automatic result eviction is performed; the harness owns retention of session data and provider logs.
- Resume restores accepted queued work. Interrupted running requests become `unknown` and are not automatically replayed. Write/yolo requests never automatically retry provider failures. Read-only requests retain classified retries; `attempts` counts these provider calls, while `attempt_id` identifies the worker dispatch.
- `cancel --request-id` targets one request. Add `--attempt-id` to require the observed running attempt. Omitting `--request-id` retains the explicit session-wide cancel operation. Ctrl+C in blocking send targets only that submit key. Cancellation of a running provider is a request, not proof of termination; inspect `lifecycle` and `termination_confirmed` before retrying.
- Session writes acquire an exclusive checkout lease shared with opt-in `mco run --write-owner <owner>`. Guarded runs serialize their invocations. Separate external worktrees can write independently; read-only sessions do not take a write lease. Legacy `run`/`review` calls without `--write-owner`, direct provider commands, and other tools do not participate in this guard. The outer harness must keep those writers away from the checkout.
- Write leases record owner, ticket reference, checkout, and base commit in `.mco/write-ownership.sqlite3`. Unconfirmed exits, crashes, and cancellation retain the lease. A matching terminal provider exit releases it. The harness must verify that the old provider and descendants have stopped, inspect the edits, then explicitly reconcile the recorded lease before authorizing another write. There is no automatic stale-lease takeover or rollback.
- Daemon readiness verifies its incarnation and worker health. A stored live PID without a matching handshake is `unknown`; stop never signals an unverified PID. Legacy daemons without incarnation receipts need manual shutdown before reuse.

`mco serve` also exposes `mco_session_submit`, `mco_session_result`, and `mco_session_cancel` over MCP, using the same clients and receipt schema. Start/ensure the named session through the CLI first. Supply a stable caller-owned `request_key` to MCP submit; after a disconnect, retry with that key or query the returned ID. Accepted work continues independently of the MCP tool call. The existing blocking `mco_run`/`mco_review` tools retain their original lifecycle; use the session tools when reconnectable submissions are required. MCP's outer `ok` envelope describes the tool call, so always inspect the execution receipt inside `data`.

Provider sandbox strength still depends on the CLI; a checkout lease is not a file sandbox. These changes have deterministic fake-provider and socket regression coverage, not a claim of universal exactly-once execution or real-provider crash robustness. See the [audit and hardening record](./audit/delegation-hardening/HARDENING.txt) for evidence and remaining work.

## Why MCO

One agent gives you one perspective. MCO turns selected agents into a review or execution team:

1. **Choose** — explicitly select the agents for the task.
2. **Dispatch** — run them in parallel, chain their work, or divide the scope.
3. **Compare** — retain each invocation's complete raw answer and operational status.
4. **Decide** — inspect evidence, disagreements, and failures before acting.

MCO keeps answer text opaque. It does not turn natural-language output into findings, severity, confidence, consensus, or an automatic decision.

For explicit review coordination, `--perspectives-json` adds a Provider-specific prompt focus. `--divide files` excludes ignored/local/build directories and round-robins the remaining sorted repository files without overlap, while `--divide dimensions` rotates review lenses in declaration order without changing target paths. These choices are visible in dry-run and arrange only the prompt or scope; the returned invocation answers remain raw.

## Built-in providers

| Provider | CLI | Provider ID |
|----------|-----|-------------|
| Claude Code | `claude` | `claude` |
| Codex CLI | `codex` | `codex` |
| Gemini CLI | `gemini` | `gemini` |
| OpenCode | `opencode` | `opencode` |
| Qwen Code | `qwen` | `qwen` |
| GitHub Copilot CLI | `copilot` | `copilot` |
| Hermes | `hermes` | `hermes` |
| Pi | `pi` | `pi` |
| [Grok Build](https://docs.x.ai/build/overview) | `grok` | `grok` |
| [Cursor CLI](https://cursor.com/docs/cli/overview) | `cursor-agent` / `agent` | `cursor` |

Each provider CLI remains responsible for its own installation, authentication, model access, and native sandbox behavior.

## Common workflows

| Goal | Command |
|------|---------|
| General multi-agent task | `mco run --providers claude,codex --prompt "..."` |
| Thin raw-answer code review | `mco review --providers claude,codex --prompt "..."` |
| Compare multiple models | `mco run --agent fast=pi:model-a --agent careful=pi:model-b --prompt "..."` |
| Preview without execution | `mco review --providers claude,pi --dry-run --json` |
| Live terminal progress | `mco review --providers claude,codex --stream live` |
| Machine-readable events | `mco review --providers claude,codex --stream jsonl` |
| File-backed chain | `mco run --agent first=pi:model-a --agent next=pi:model-b --chain --result-mode artifact` |
| Debate and synthesis | `mco review --providers claude,codex --debate --synthesize --result-mode both` |
| Discover provider models | `mco agent models --providers codex,pi --json` |

Pin models for one run without changing provider CLI defaults:

```bash
mco review \
  --providers codex,pi \
  --provider-models-json '{"codex":"gpt-5.4","pi":{"provider":"seal","model":"deepseek-v4-pro"}}' \
  --prompt "Review this repository for bugs."
```

## Permissions and safety

MCO translates one execution profile into each provider's native flags:

| Mode | Intended use | Default |
|------|--------------|---------|
| `read_only` | Inspect and review without workspace mutation | `mco review` |
| `write` | Create and edit workspace files | `mco run` |
| `yolo` | Use the provider's broadest bypass profile | Explicit opt-in only |

Important boundaries:

- `--allow-paths` validates MCO's requested scope; it is not an operating-system sandbox.
- Provider sandbox strength depends on the underlying CLI.
- Hermes oneshot bypasses approvals and therefore requires explicit `--execution-mode yolo`.
- ACP terminal access is a trusted-agent capability. Use isolation for untrusted agents or prompts.
- MCO does not create or manage worktrees. If the user selects parallel writers, partition ownership with non-overlapping `--target-paths` and warn about edit conflicts.

See [Provider and permission reference](./docs/reference/providers.md) for the complete mapping.

## Use MCO from another agent

MCO's CLI is self-describing. A calling agent can read `mco -h`, resolve saved defaults, confirm the provider/model team with the user, preview the policy, and then execute.

> “Use MCO to run a security review with Claude and Codex, and an architecture review with Pi.”

The installer and runtime use two different selections:

- Installer `--agent` chooses which calling agents receive the MCO Skill.
- Runtime `--providers`, saved `providers` config, or runtime `--agent` declarations choose the task invocations.

```bash
npx @tt-a1i/mco@latest install --agent codex --agent claude-code --yes
mco doctor --skill-health --json
```

## How it works

```text
You or a calling agent
        │
        ▼
  mco run / review
        │
        ├── Claude ──┐
        ├── Codex    │
        ├── Gemini   ├──► raw answers / file-backed stages ──► output
        ├── Pi       │
        └── ...   ───┘
                              │
                       text · JSON · JSONL · Markdown artifacts
```

Provider processes are isolated behind a shared adapter contract: detect, run, poll, cancel, and transport decode. One invocation failure does not discard successful provider answers.

## Documentation

| Topic | Guide |
|-------|-------|
| Installation, first run, and common workflows | [Workflow guide](./docs/guides/workflows.md) |
| Providers, models, and permission mappings | [Provider reference](./docs/reference/providers.md) |
| CLI flags, outputs, artifacts, and exit codes | [CLI reference](./docs/reference/cli.md) |
| Config files and custom agents | [Configuration reference](./docs/reference/configuration.md) |
| Machine-readable error contract | [Error contract](./docs/contracts/errors-v0.1.x.md) |
| Invocation and artifact contract | [Invocation contract](./docs/contracts/invocation-runtime-v1.md) |
| Provider permission contract | [Permission contract](./docs/contracts/provider-permissions-v0.1.x.md) |
| Release process | [RELEASING.md](./RELEASING.md) |
| Release history | [CHANGELOG.md](./CHANGELOG.md) |

Run `mco <command> --help` for the authoritative option list installed with your version.

## Development

```bash
git clone https://github.com/ShredFred/mco.git
cd mco
python3 -m pip install -e .
python3 -m unittest discover -s tests -p 'test_*.py'
npm test
```

## License

MIT — see [LICENSE](./LICENSE).
