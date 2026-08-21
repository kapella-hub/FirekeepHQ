---
name: install-firekeep
description: Installs and connects Firekeep, a self-hosted persistent memory and working-context layer for AI agents, then verifies it with the product's own health check. Use this skill when the user asks to install, set up, or connect Firekeep; when Firekeep's tools are missing, failing, or not connected to a server; or when they want their agent to remember decisions, context, and past work across sessions, machines, and different agents — even if they do not mention Firekeep by name.
---

# Install Firekeep

Firekeep is a self-hosted memory and working-context layer for AI agents. It has
two halves: a **server** (runs once, on a machine the user controls) and a
**client kit** (installed on each machine that talks to it). Your job is to get
this machine's client kit installed and connected, then prove it worked.

Everything below wraps commands the product already ships. Do not reimplement
the installer, hand-edit `~/.firekeep/config`, or guess at values — run the real
commands and read their real output.

## Step 1 — check before you install

```
firekeep doctor
```

- **Command not found** → the client kit is not installed. Go to Step 2.
- **It runs and every row is `[OK]`** → Firekeep is already installed and
  connected. **Stop here** and tell the user. Do not reinstall.
- **It runs but rows are `[FAIL]`/`[WARN]`** → the kit is installed but
  something is wrong. Skip to Step 5; doctor's output names the fix.

## Step 2 — install the client kit

One command, and it is the same command whether or not the user has a server
yet. It installs the kit only.

macOS / Linux:
```
curl -fsSL https://firekeep.ai/latest/install | sh
```

Windows (PowerShell):
```
irm https://firekeep.ai/latest/install.ps1 | iex
```

This provisions a standalone Python into `~/.firekeep`, installs the wheels, and
renders MCP configuration for the agent runtimes it supports — Claude Code,
Codex, Kiro and OpenCode, plus Claude Desktop when that app is installed.
Because your shell is not an interactive terminal,
the installer runs non-interactively: **it installs the client and connects
nothing.** That is the intended outcome at this step — connecting is Step 4,
after you have asked the user a question only they can answer.

The `firekeep` command lands in `~/.firekeep/shims`. If a later step reports
`command not found`, start a new shell or call it by full path
(`~/.firekeep/shims/firekeep`) rather than editing the user's shell profile.

## Step 3 — ask the user where their server is

**Ask, and wait for an answer. Do not choose for them.** This decides whether a
multi-container server gets provisioned on this machine, and only the user
knows which applies. Present exactly these four:

1. **Set one up on this machine** — installs the server here (requires Docker).
2. **I have a join code** — from a teammate, or the Dashboard's Devices tab.
3. **It is already running** — they know its address and have an API key.
4. **Not yet** — finish the client now, connect later.

## Step 4 — connect, according to their answer

**Answer 2 — join code:**
```
firekeep join <code>
```

**Answer 3 — an already-running server:** ask for the host address, then:
```
firekeep install --non-interactive --host <host>
```
If they have an API key to supply, tell them doctor will report an auth failure
until it is set, and let them place it — do not ask them to paste a key into
the chat.

**Answer 4 — not yet:** stop. The kit is installed and does nothing until
connected. Tell them the three ways to finish later: `firekeep init` (here),
`firekeep join <code>`, or `firekeep connect <user@host>` (over SSH).

**Answer 1 — set up the server here. Confirm before running this.**

`firekeep init` is not a config change. It provisions a real multi-container
stack on this machine — Neo4j, Qdrant, Redis and Ollama — pulls several GB of
images and a model, generates database passwords, and starts long-running
containers. It takes minutes, not seconds.

Before running it, tell the user plainly what it will do (that list above) and
**get an explicit yes.** Also confirm Docker is installed and running — the
command needs it.

```
firekeep init
```

This is a request the skill makes of you, not a lock the product enforces: no
instruction file can compel a model. Honor it anyway. Provisioning infrastructure
on someone's machine without asking is the kind of action that should never be a
side effect of "install this for me."

## Step 5 — verify, and show the real output

```
firekeep doctor
```

Show the user doctor's actual rows. Do not summarize it as "installed
successfully" — doctor is the thing that decides whether it worked, so let it
speak.

Common results and what they mean:

- **`This machine has a Firekeep client but no server to talk to`** — expected
  after answer 4. Not an error; the row names the three ways to finish.
- **Four services failing to connect at once** — the server is not reachable
  (not started, wrong address, or a firewall). Not four separate problems.
- **`[WARN] embeddings`** — the server is up but still pulling its model
  (~3.3 GB). Memories written now are stored but not yet searchable. Wait and
  re-run; it resolves itself.
- **`[WARN] client-version`** — a newer client exists. `firekeep update`.

Anything else: the row's own text names the repair command. Run it, then run
doctor again.

## Out of scope — do not do these for the user

- **Do not choose which folders or mailboxes Firekeep reads.** The commands
  `firekeep docdex add` and `firekeep maildex add` are deliberately human-only:
  which documents an agent may read is a privacy decision, so no agent-callable
  tool for it exists. Explain the commands; let the user run them.
- **Do not paste credentials into the conversation.** Join codes are single-use
  and short-lived; API keys and passwords belong in the user's own config, not
  in a transcript.
- **Do not `firekeep uninstall --server`** as cleanup after a failed install. It
  deletes all data with no undo. A failed install leaves a working machine;
  diagnose with doctor instead.

## Reference

- Install and operations docs: https://firekeep.ai/docs.html
- What Firekeep understands (code, documents, mail): https://firekeep.ai/dexes.html
- Privacy notice: https://firekeep.ai/privacy.html
