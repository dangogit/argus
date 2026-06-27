#!/usr/bin/env sh
set -eu

REPO_URL="${ARGUS_REPO_URL:-https://github.com/dangogit/argus.git}"
PACKAGE_SPEC="${ARGUS_PACKAGE_SPEC:-git+$REPO_URL}"
PYTHON_BIN="${ARGUS_PYTHON:-}"
DRY_RUN="${ARGUS_INSTALL_DRY_RUN:-}"

log() {
  printf '%s\n' "$*"
}

run() {
  printf '+'
  for arg in "$@"; do
    printf ' %s' "$arg"
  done
  printf '\n'
  if [ -z "$DRY_RUN" ]; then
    "$@"
  fi
}

python_ok() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

find_python() {
  if [ -n "$PYTHON_BIN" ]; then
    if command -v "$PYTHON_BIN" >/dev/null 2>&1 && python_ok "$PYTHON_BIN"; then
      return 0
    fi
    log "argus installer: ARGUS_PYTHON is not Python 3.11+: $PYTHON_BIN" >&2
    return 1
  fi

  for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
      PYTHON_BIN="$candidate"
      return 0
    fi
  done

  log "argus installer: Python 3.11+ not found" >&2
  log "Install Python 3.12 or 3.11, then rerun. On macOS: brew install python@3.12" >&2
  return 1
}

install_with_pipx() {
  if command -v pipx >/dev/null 2>&1; then
    run pipx install --force --python "$PYTHON_BIN" "$PACKAGE_SPEC"
    return 0
  fi

  log "argus installer: pipx not found, installing pipx for current user"
  run "$PYTHON_BIN" -m pip install --user --upgrade pipx
  run "$PYTHON_BIN" -m pipx install --force --python "$PYTHON_BIN" "$PACKAGE_SPEC"
  run "$PYTHON_BIN" -m pipx ensurepath
}

main() {
  find_python
  if ! command -v git >/dev/null 2>&1; then
    log "argus installer: git not found. Install git first." >&2
    return 1
  fi

  log "argus installer: using $PYTHON_BIN"
  log "argus installer: installing $PACKAGE_SPEC"
  install_with_pipx

  log ""
  log "Argus installed. Restart your shell if argus is not on PATH, then run:"
  log "  argus --version"
  log "  argus doctor"
}

main "$@"
