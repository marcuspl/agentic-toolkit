#!/usr/bin/env bash
#
# install.sh — put swarm-review on PATH and register its skills, sourced from
# THIS repo (GitHub-backed) rather than a synced dotfiles repo.
#
# Idempotent. Safe to re-run. Portable to Linux + macOS.
#
#   BIN_DIR     where to symlink the engine   (default: ~/.local/bin)
#   SKILLS_DIR  where to register the skills   (default: ~/.claude/skills)
#
# If SKILLS_DIR is itself a symlink (e.g. the keybase setup where
# ~/.claude/skills -> .../dotfiles/.claude/skills), it is PROMOTED to a real
# local directory first: every skill that was reachable through it is re-linked
# back to its origin, then the swarm-review skills are (re)pointed at this repo.
# Net effect: the swarm-review skills no longer depend on keybase; any other
# skills keep working exactly as before (still sourced from wherever they live).
# Revert by: rm -rf "$SKILLS_DIR" && ln -s <old-target> "$SKILLS_DIR".

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENGINE="$SCRIPT_DIR/swarm-review"
SKILLS_SRC="$REPO_ROOT/.claude/skills"
SWARM_SKILLS=(swarm-review-code swarm-review-prose swarm-review-generate)

BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
SKILLS_DIR="${SKILLS_DIR:-$HOME/.claude/skills}"

log() { printf '  %s\n' "$*"; }
say() { printf '\n%s\n' "$*"; }

# ln that replaces an existing symlink/dir-symlink atomically on both GNU and
# BSD ln (-s symbolic, -f force, -n don't deref an existing dir symlink target).
relink() { ln -snf "$1" "$2"; }

[ -x "$ENGINE" ] || { echo "error: engine not found/executable at $ENGINE" >&2; exit 1; }

say "swarm-review install"
log "repo:   $REPO_ROOT"

# ── 1) Engine on PATH ─────────────────────────────────────────────────────────
mkdir -p "$BIN_DIR"
relink "$ENGINE" "$BIN_DIR/swarm-review"
log "engine: $BIN_DIR/swarm-review -> $ENGINE"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) log "WARNING: $BIN_DIR is not on your PATH. Add it, e.g.:"
     log "         echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc" ;;
esac

# ── 2) Skills directory (promote off a symlink if needed) ─────────────────────
if [ -L "$SKILLS_DIR" ]; then
  OLD_TARGET="$(cd "$SKILLS_DIR" 2>/dev/null && pwd -P || true)"
  say "skills dir is a symlink -> ${OLD_TARGET:-?}"
  log "promoting it to a real local directory (keybase-independent)"
  # Snapshot reachable skills before detaching the symlink.
  names=(); srcs=()
  if [ -n "$OLD_TARGET" ] && [ -d "$OLD_TARGET" ]; then
    for d in "$OLD_TARGET"/*/; do
      [ -d "$d" ] || continue
      names+=("$(basename "$d")"); srcs+=("${d%/}")
    done
  fi
  rm "$SKILLS_DIR"
  mkdir -p "$SKILLS_DIR"
  for i in "${!names[@]}"; do
    relink "${srcs[$i]}" "$SKILLS_DIR/${names[$i]}"
  done
  log "re-linked ${#names[@]} existing skill(s) into $SKILLS_DIR"
else
  mkdir -p "$SKILLS_DIR"
fi

# ── 3) Point the swarm-review skills at THIS repo ─────────────────────────────
say "registering swarm-review skills (source: this repo)"
for name in "${SWARM_SKILLS[@]}"; do
  [ -d "$SKILLS_SRC/$name" ] || { log "skip $name (missing in repo)"; continue; }
  relink "$SKILLS_SRC/$name" "$SKILLS_DIR/$name"
  log "skill:  $SKILLS_DIR/$name -> $SKILLS_SRC/$name"
done

say "done. verify:  swarm-review --help   (and /swarm-review-generate in Claude Code)"
