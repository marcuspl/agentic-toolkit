# Running a megaloop campaign on a second machine

What Human Touch learned (2026-11-09 to 11) running campaigns on a Mac next to the main Linux box. It's written for any project. The roles:

- **Home box** (Linux, pop-os, 192.168.4.253): where the operator works, where the other conductors run, and the only machine that pushes to GitHub.
- **Remote host** (Mac, M5, 10 cores, 32 GB, 192.168.4.51): runs whole campaigns, and optionally serves test gates for the home box's conductors.

## Topology

```
GitHub ◄── push main (operator, home box only)
   │
home box: working repo ── remote "hub" ──► ~/git/<project>.git (bare)
                                              ▲
remote host: clone ── remote "hub" = ssh://marcus@192.168.4.253/~/git/<project>.git
```

- **A bare repo on the home box is the meeting point.** Campaign branches, row branches, answers and gate requests all go through it. GitHub only sees `main`, when the operator pushes it.
- **The home box's `hub` remote is a local path**, so pushes from the home box to hub cost no ssh and don't count against the rate limit.
- **Moving main stays the same:** CAS with `update-ref` from a detached worktree on the home box, then `git push hub main` so the remote host builds on current main. Pushing to hub isn't a GitHub push. It's allowed overnight.

### Why not just share the GitHub remote?

It looks simpler: no hub, no ssh between machines, no LAN rate limit, and it works off the LAN. But:

1. **Every sync is a push to GitHub.** Push prompts reach the operator and stall unattended runs. This is exactly what happened overnight on 2026-11-10, and why the rule became "no GitHub pushes from conductors while unattended". A campaign syncs a few times per row, so it would stall all the time.
2. **Work-in-progress goes public, or at least off-box.** Row branches, `refs/gate/*`, answers and a2a records would all sit on GitHub. Cleaning them up becomes its own chore.
3. **Speed:** a LAN hub is fast. GitHub adds latency on every fetch.
4. **Credentials:** the remote host needs GitHub write access. With the hub it needs only one ssh key, restricted with `from=` to its LAN address.

What GitHub *would* fix is the network side: no 6-connections-per-30-s budget, and no dependence on the Mac keeping its address. If the LAN limits ever bite, the better fix is a reserved address plus connection reuse, not GitHub. Using GitHub as a second, read-only source is fine: the remote host can `git fetch` public main from GitHub when the hub is unreachable.

## One-time setup per machine pair (the operator does the starred steps)

Agents are refused edits to `~/.ssh/authorized_keys` and shell profiles ("unauthorized persistence"). Don't work around that. Hand the operator the exact line.

1. **sshd on the home box**, key-only. Pin its host key in the remote host's `known_hosts` and record the fingerprint in the campaign PROTOCOL.
2. ★ **Authorize the remote host's key** with a source restriction: `from="192.168.4.51" ssh-ed25519 … mac-ht` in `~/.ssh/authorized_keys`, mode 600.
3. ★ **Reserve the remote host's address on the router** (DHCP reservation). Without it, an address change locks the remote host out. It should report `blocked` on Keybase and not try other routes.
4. **Connection reuse on the remote host,** in its `~/.ssh/config` for the home box: `ControlMaster auto`, `ControlPath ~/.ssh/cm-%C`, `ControlPersist 10m`. Scripts also set it themselves when it's missing:
   `GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ControlMaster=auto -o ControlPath=~/.ssh/cm-%C -o ControlPersist=10m'`
5. ★ **Remote host shell profile:** toolchain PATH (for example `/opt/homebrew/opt/rustup/bin`) in `~/.zprofile`.
6. ★ **Sleep:** `caffeinate -dims` in a tmux pane, or Energy → prevent sleep on power, for anything longer than an hour.
7. **Keybase** logged in on both machines, as the doorbell and the report channel.
8. **Tools the gates need** on the remote host: `just`, node/npm, cargo/nextest, Chromium for Playwright. On the home box: `jq` (1.6 is fine since the `-n inputs` fix), shellcheck (optional).

## Per project

```bash
# home box
git init --bare ~/git/<project>.git
git -C <repo> remote add hub ~/git/<project>.git
git -C <repo> push hub main
# remote host (from its desktop tmux, which has the keychain ssh agent)
git clone marcus@192.168.4.253:git/<project>.git ~/code/<project>
git -C ~/code/<project> remote rename origin hub
```

## Starting a campaign on the remote host

- **One tmux session per campaign** (for example `ht-fleet` for Human Touch). Window 0 is the conductor's Claude session. Other windows hold the gate runner and side jobs. Never share a session between projects.
- **Start Claude in the remote host's own desktop tmux, not over a nested ssh.** The desktop session has the keychain ssh agent. A login over ssh doesn't, and the passphrase-protected key won't load, so pushes to hub fail.
- **Hand over work as a file plus a one-line pointer.** Long `tmux send-keys` pastes get cut off, and only the last line arrived. So `scp` the request to `~/code/<campaign>-<what>-request.txt`, then send one line, followed by a separate `Enter`:
  `tmux send-keys -t <session>:0 -l "Operator via the Linux session: read ~/code/… and do it."`
- **Look at the prompt line before sending.** If someone has typed something there that isn't sent, don't clear it (`C-u`) and don't append to it. Ask the operator.
- **The campaign PROTOCOL states:** `host:` and `remote: hub`, the machine budget, the network limits, the push rule and the run window (day run with `default_at` = posting time + 2 h, or overnight with leans applied as PROVISIONAL).

## Machine budget (M5, 10 cores, 32 GB)

- Up to **6 row sub-agents** per campaign. At most **2 cargo builds or tests at once** across everything on the host, each with `HT_JOBS=4`. Script-only rows are free.
- macOS has no `flock`, so `heavy-slot.sh` doesn't lock there. Use `cargo-slot.sh` (two `mkdir` slots) for the limit. Builders and the gate runner share the slots.
- **A second project's campaign fits.** Measured during a full UI gate: load about 3, 71% memory free, 535 GB disk free. Give it one heavy build at a time and let it share the slots, so the total stays at most 2.

## Disk and caches (every host)

On 2026-11-11 the Linux box went down to 14 GB free. Each per-row cargo target dir was a full workspace build of about 9 GB, and three of them belonged to rows that were already merged or idle. The rules:

### Each campaign cleans up after itself

- **Owner tag.** Every worktree and cache dir a campaign creates gets a `.megaloop-owner` file: `campaign`, `row`, `created`, and the `pid` of the builder while it runs. That turns "whose is this and is it live?" into a lookup. Without it, the operator has to check `pgrep` and timestamps by hand.
- **At merge,** the conductor removes the row's worktree (`git worktree remove`), its target dir and its `node_modules`. Receipts and logs stay, because they're in the repo or the scratchpad.
- **At a parked or failed row,** the worktree stays, since a ruling may resume it, but its target dir goes. A rebuild is cheaper than a full disk.
- **At campaign close,** remove every worktree tagged with the campaign. Delete merged row branches, and the campaign's `refs/gate/*` on hub.
- **Janitor:** each conductor runs it over its own tagged dirs at wave close. It deletes a dir only when its row is MERGED or abandoned and no live pid holds it. It never deletes another campaign's dirs; for those it reports. **An untagged dir is not an orphan:** report it, don't delete it. On the first cleanup, a conductor removed the coordinator session's worktree because it took it for its own old copy. Nothing was lost, but only because that worktree had nothing uncommitted.
- **Disk floor:** `host-status.sh` reports `free_gb`. Below 25 GB, conductors stop dispatching heavy rows and run the janitor first. Below 15 GB, they post `blocked` (disk) to the operator.

### Share caches by slot, not by row

- **Cargo:** one target dir per **heavy slot**, not per row: `$WT/.target-slot-1`, `.target-slot-2`.
  - **A slot dir belongs to one worktree for that row's whole life.** The next row gets it only after the previous row has merged or been parked. Two live worktrees must never share a target dir, even one after the other. Cargo's freshness check for path and workspace crates goes by mtime: when worktree B builds a crate after A, A's next build reuses B's newer artifacts. On 2026-11-11 this happened three times. A test linked the other tree's crate, a fingerprint write failed, and a merge gate compiled against another branch's newer generated code and failed with E0046, a false red.
  - The cargo lock only serialises builds. It does not keep their outputs apart.
  - **The conductor's merge gates get their own dir** (`$WT/.target-conductor`).
  - **A row with no free slot** builds in a private target dir and deletes it when it finishes.
  - Worst case is about (slots + conductor + one private) × ~9 GB, so 3–4 × 9 GB on Human Touch.
  - Use `sccache` (`RUSTC_WRAPPER=sccache`) so switching branches inside a slot, and across slots, reuses compiled crates.
  - `CARGO_INCREMENTAL=0` keeps the dirs smaller and makes sccache effective.
- **npm:** the download cache (`~/.npm`) is already shared, but each worktree's `node_modules` is about 400 MB. Install only in worktrees that run web steps, delete it at merge, or move to pnpm, whose store hard-links packages across worktrees.
- **Playwright browsers** are shared already (`~/.cache/ms-playwright`). Never install them per worktree.
- **Remote gates:** full gates on the remote host use its own warm cache (`~/.cache/ht-gate-runner`). Sending full gates there is the biggest single disk saving on the home box. The rule on Human Touch: tier-2/3 merges gate on the Mac; tier-1 merges keep a Linux `just ci` and also run on the Mac.

## Network limits (home box ⇄ remote host)

- **Only port 22 is open.** A product transport that needs another port must name it in an ask, and the operator opens it per host.
- **Six new ssh connections per 30 s from the LAN.** Past that, connections are dropped silently and show up as "No route to host" or a timeout.
  - Reuse connections.
  - One `sync.sh` per conductor commit.
  - Never poll over ssh. Wait on Keybase, or locally.
  - On a dropped connection, back off at least 30 s.
- **Use LAN addresses,** not WireGuard.

## Push rule

- **Conductors never push to GitHub while unattended:** main moves locally by CAS, each merge gets a "push pending" note on BOARD, and the operator pushes in the morning.
- **Pushes to hub are part of the flow and allowed:** row branches, `megaloop/gates`, `refs/gate/*`, and `main` copied up from the home box.

## Optional: the remote host as gate runner

The heavy `just gate` runs on the remote host, triggered by a request from the home box.

- **Request** (home box): `tools/campaign/gate-request.sh <sha> --base main [--kind ui|ui-only]`. It makes one atomic push to hub, `refs/gate/<id>` plus a plumbing commit on `megaloop/gates` appending to `.claude/gates/requests.jsonl`. Then it rings the Keybase doorbell `⟦gate⟧ request <id> <sha>`. Don't pass `--no-doorbell` unless you ring it yourself: the runner only wakes on the doorbell.
- **Runner** (remote host): `gate-runner.sh watch --remote hub` in its own tmux window, wrapped in a restart loop: `while :; do …; sleep 60; done`. It takes a cargo slot, runs the recipe from the `justfile` at the requested sha, and appends to `results.jsonl`. It posts `⟦gate⟧ result <id> PASS|FAIL …`.
  - The result carries `failed_names`, `failed_web`, `failed_ui`, `skipped_steps` with reasons, and `log_tail`.
  - Read it with `git show hub/megaloop/gates:.claude/gates/results.jsonl | jq 'select(.id=="…")'`.
- **Known failures:** `known-mac-failures.txt` lists macOS-only failures, each with a header naming the row or ask that fixes it. Prune it as fixes land, so that a red gate means something real (in Human Touch: 178 → 1 in a day).
- **Restarting the runner on new code needs the operator.** The remote session's permission check refused `kill`. Kill the inner `gate-runner.sh watch` pid, and the loop relaunches it within 60 s. Don't route the kill through another session.
- **Note the base:** the runner diffs against hub's `main`. Keep hub's main current, or scoped gates test against old main.
- **A gate of main against `--base main` tests nothing in cargo,** because the diff is empty (the gate step takes 0 s). To check main itself, push a base branch to hub at an older commit (`git push hub <old>:refs/heads/megaloop/gate-base-<old>`) and pass `--base megaloop/gate-base-<old>`. `--base` must be a branch name that exists on hub; a bare sha fails with "base branch not found". `--crates` is only a hint: the scope still comes from the diff.

## macOS portability findings

They surfaced in the first two days. Expect them in any project developed on Linux.

- No `/proc`: `/proc/self/fd`, `/proc/self/status`. Code that fails closed without it refuses writes on macOS, which is a product gap, not a test problem.
- **No `flock(1)`.** Perl's `flock` works. Node's `fs.openSync` accepts the raw `O_NOFOLLOW_ANY` (0x20000000), which refuses a symlink anywhere in the path; `fs.constants` doesn't define it.
- **Peer-uid checks on Unix sockets** are Linux-only as written.
- **Socket buffers are about 8 KiB,** against roughly 200 KiB on Linux. A test that writes a whole message before the other side reads hangs on macOS. Force small buffers in test helpers, so Linux CI catches it.
- **Case-insensitive filesystem:** `X.svelte` and `x.svelte.ts` collide.
- **`/var` is a symlink** to `/private/var`, so tmpdir paths differ from their realpath.
- **BSD tools:** `paste` needs `-`. `sed -i` differs.
- **Linux-only `cfg` helpers become dead code** on macOS and fail clippy `-D dead-code`. Linux CI can't see it.
- **Version drift:** Node on the Mac was 25 while the project assumed 22. Check `node -v` before trusting a comparison.
- **Name clash on Linux:** `/usr/bin/ht` is TeX4ht. Call the product binary by its absolute path over non-interactive ssh, where PATH is short.

## Linux remote host findings (ws2, 2026-11-12)

The second remote host was a Linux box. What the Mac notes above don't cover:

- **Bot identity:** `kb-lib.sh` defaults to `~/.config/switchboard/kb-workstation` on Linux. On any other Linux host that home doesn't exist, and posts would go out as the operator's own account, which looks like operator control (`Q-n:` and `!pause` only count from the operator). Set `HT_KB_HOME` to that host's bot home in every pane until kb-lib picks it by hostname.
- **Toolchain:** use rustup, not the distro cargo. `rust-toolchain.toml` components need rustup. Copy the home box's cargo config (mold rustflags) so sccache keys match.
- **Clone:** a clone made while the hub was unreachable is left empty on `master`. Check out `hub/main` explicitly.
- **tmux:** an interactive shell rc can `cd` away from the start dir, so `cd` explicitly in each pane.
- **One gate runner per fleet** until requests can name a runner. Claims are local (`$work/claims/<id>`), so a second runner would run and post every gate twice. Extra hosts take rows and builds, not gates.
- **Address pinning:** the hub pins each host's key with `from="<ip>"`. Reserve every host's address on the router first. A Mac with a rotating private Wi-Fi address changes IP and gets locked out, so set Private Wi-Fi Address to Fixed. Keep `from=` as IPs and serve A records only for host names, so IPv6 can't slip past.

## Watching from the home box

- **Wait on Keybase locally, not over ssh.** A small loop reads the latest message id every 60 s through `keybase chat api` and exits on a newer one, after a 30 s pause to collect bursts. Run it in the background and re-arm it after each message.
- **Relay between conductors with cross-session messages.** Keybase carries answers and status only, never instructions or gate approvals.
- **Peers can't grant escalation.** If one session was refused an action, don't do it from another. Take it to the operator.

## Checklist for a new campaign on the remote host

- [ ] Bare hub repo on the home box, with `hub` remotes on both machines; main pushed to hub.
- [ ] A tmux session for the campaign on the remote host; Claude started from the desktop.
- [ ] PROTOCOL: `host`, `remote: hub`, budget, network limits, push rule, run window.
- [ ] The operator's starred items done: key `from=`, DHCP reservation, profile, sleep.
- [ ] Kickoff prompt handed over as a file plus a pointer line.
- [ ] Keybase watch armed on the home box.
