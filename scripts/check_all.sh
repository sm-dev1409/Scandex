#!/usr/bin/env bash
#
# Verify the whole repository - backend and frontend - with one command.
#
#     ./scripts/check_all.sh
#     bash scripts/check_all.sh          # Windows / Git Bash
#     SKIP_FRONTEND=1 ./scripts/check_all.sh
#
# A shell script rather than an `npm run check`: there is no root package.json
# (the only Node project is scanner-frontend/), so inventing one just to hold a
# script would misrepresent the repo's shape.
#
# Every step runs even if an earlier one fails, and the failures are summarised
# at the end - a lint error should not hide a broken test. The exit code is
# non-zero if any REQUIRED step failed.
#
# NETWORK: only the last step touches the network, and it is advisory. Without
# C8_CLIENT_SECRET, `check_cantor8.py --summary` reports FAIL/SKIP for the
# live-ledger checks; that is the expected, correct output for an unconfigured
# clone and does NOT fail this script. Everything else is fully offline.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"
FRONTEND="$ROOT/scanner-frontend"

# Colours only when attached to a terminal.
if [ -t 1 ]; then
  R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; B=$'\033[1m'; N=$'\033[0m'
else
  R=""; G=""; Y=""; B=""; N=""
fi

FAILED=()
WARNED=()
PASSED=()

# run <label> <command...>   - a failure fails the script
run() {
  local label="$1"; shift
  printf '\n%s══ %s ══%s\n' "$B" "$label" "$N"
  if "$@"; then
    PASSED+=("$label"); printf '%s✔ %s%s\n' "$G" "$label" "$N"
  else
    FAILED+=("$label"); printf '%s✘ %s%s\n' "$R" "$label" "$N"
  fi
}

# advisory <label> <command...>  - a failure warns but does not fail the script
advisory() {
  local label="$1"; shift
  printf '\n%s══ %s (advisory) ══%s\n' "$B" "$label" "$N"
  if "$@"; then
    PASSED+=("$label"); printf '%s✔ %s%s\n' "$G" "$label" "$N"
  else
    WARNED+=("$label"); printf '%s! %s - non-fatal%s\n' "$Y" "$label" "$N"
  fi
}

PY="${PYTHON:-python}"

# ── backend ────────────────────────────────────────────────────────────────
run "compile (python)" "$PY" -m compileall -q src scripts tests

if "$PY" -m pytest --version >/dev/null 2>&1; then
  run "tests (pytest)" "$PY" -m pytest -q
else
  printf '%spytest not installed; falling back to unittest%s\n' "$Y" "$N"
  run "tests (unittest)" "$PY" -m unittest discover -s tests
fi

# ── frontend ───────────────────────────────────────────────────────────────
if [ "${SKIP_FRONTEND:-0}" = "1" ]; then
  printf '\n%sSKIP_FRONTEND=1 - skipping the frontend checks%s\n' "$Y" "$N"
elif ! command -v npm >/dev/null 2>&1; then
  WARNED+=("frontend (npm not found)")
  printf '\n%snpm not found - skipping the frontend checks%s\n' "$Y" "$N"
elif [ ! -d "$FRONTEND/node_modules" ]; then
  WARNED+=("frontend (deps not installed)")
  printf '\n%sscanner-frontend/node_modules missing.%s\n' "$Y" "$N"
  printf 'Run: cd scanner-frontend && npm install\n'
else
  run "frontend lint"  npm --prefix "$FRONTEND" run --silent lint
  run "frontend tests" npm --prefix "$FRONTEND" run --silent test:run
  run "frontend build" npm --prefix "$FRONTEND" run --silent build
fi

# ── live ledger (advisory: needs C8_CLIENT_SECRET + network) ────────────────
if [ -z "${C8_CLIENT_SECRET:-}" ]; then
  printf '\n%s══ cantor8 summary (advisory) ══%s\n' "$B" "$N"
  printf '%sC8_CLIENT_SECRET is not set - skipping the live-ledger check.%s\n' "$Y" "$N"
  printf 'This is expected on a fresh clone. Everything above is offline and complete.\n'
  WARNED+=("cantor8 summary (no C8_CLIENT_SECRET)")
else
  advisory "cantor8 summary" "$PY" scripts/check_cantor8.py --summary
fi

# ── report ─────────────────────────────────────────────────────────────────
printf '\n%s══ summary ══%s\n' "$B" "$N"
for s in "${PASSED[@]:-}";  do [ -n "$s" ] && printf '%s  PASS%s  %s\n' "$G" "$N" "$s"; done
for s in "${WARNED[@]:-}";  do [ -n "$s" ] && printf '%s  WARN%s  %s\n' "$Y" "$N" "$s"; done
for s in "${FAILED[@]:-}";  do [ -n "$s" ] && printf '%s  FAIL%s  %s\n' "$R" "$N" "$s"; done

if [ "${#FAILED[@]}" -gt 0 ]; then
  printf '\n%s%d check(s) failed.%s\n' "$R" "${#FAILED[@]}" "$N"
  exit 1
fi
printf '\n%sAll required checks passed.%s\n' "$G" "$N"
exit 0
