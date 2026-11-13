#!/usr/bin/env bash
set -euo pipefail

repo="$(cd -- "$(dirname -- "$0")" >/dev/null && pwd -P)"
claude_dir="$HOME/.claude"
links=(
  "$repo|$claude_dir/tools/agent-sessidx"
  "$repo/skills/agent-sessidx|$claude_dir/skills/agent-sessidx"
)
codex_dir="${CODEX_HOME:-$HOME/.codex}"
if [ -d "$codex_dir" ]; then
  links+=("$repo/skills/agent-sessidx|$codex_dir/skills/agent-sessidx")
fi

for pair in "${links[@]}"; do
  target="${pair%%|*}" path="${pair#*|}"
  if [ -L "$path" ] && [ "$(readlink "$path")" != "$target" ]; then
    echo "error   $path is a symlink to $(readlink "$path"); move it away and rerun" >&2
    exit 1
  fi
  if [ -e "$path" ] && [ ! -L "$path" ]; then
    echo "error   $path already exists; move it away and rerun (nothing is overwritten)" >&2
    exit 1
  fi
done

for pair in "${links[@]}"; do
  target="${pair%%|*}" path="${pair#*|}"
  if [ -L "$path" ]; then
    echo "ok      $path"
  else
    mkdir -p "$(dirname "$path")"
    ln -s "$target" "$path"
    echo "linked  $path -> $target"
  fi
done

python3 -I "$claude_dir/tools/agent-sessidx/agent-sessidx.py" stats
