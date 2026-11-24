#!/usr/bin/env bash
# Install the lean Claude Code launcher.
#
#   install_lean.sh [dest]      default: $HOME/plan-omp-lean
#
# Existing plans in <dest>/state are kept. Set PLAN_OMP_BIN to link the commands
# somewhere other than $HOME/.local/bin.
set -eu

here="$(cd "$(dirname "$(realpath "$0")")" && pwd)"
src="$here/src"
dest="$(realpath -m "${1:-$HOME/plan-omp-lean}")"
bin="${PLAN_OMP_BIN:-$HOME/.local/bin}"

case "$dest/" in
  "$HOME/.local/"*|"$HOME/.config/"*|"$HOME/.ssh/"*|"$HOME/.aws/"*|"$HOME/.gnupg/"*|"$HOME/.kube/"*|"$HOME/.docker/"*)
    echo "Choose another folder. CC GUI blocks tool calls that use $dest." >&2
    exit 2
    ;;
esac

mkdir -p "$dest" "$bin"
cp "$src/scripts/omp_exec.py" "$src/scripts/omp_watch.py" "$src/lean/plan-omp-lean" "$dest/"
chmod 755 "$dest/omp_exec.py" "$dest/omp_watch.py" "$dest/plan-omp-lean"
python3 -I "$src/lean/build_system.py" "$src" "$dest"
ln -sfn "$dest/plan-omp-lean" "$bin/plan-omp-lean"
ln -sfn "$dest/plan-omp-lean" "$bin/plan-omp-lean-cc"
ln -sfn "$dest/omp_watch.py" "$bin/plan-omp-lean-watch"

echo "Installed: $dest"
echo "Commands: plan-omp-lean, plan-omp-lean-cc, plan-omp-lean-watch in $bin"
