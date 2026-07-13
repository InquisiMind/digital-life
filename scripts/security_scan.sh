#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORT_DIR="${SECURITY_REPORT_DIR:-$ROOT_DIR/var/security}"
SEMGREP_VENV_BIN="$HOME/.local/share/digital-life-tools/semgrep/bin/semgrep"
RUN_HISTORY="${SECURITY_SCAN_HISTORY:-1}"

usage() {
  cat <<'USAGE'
Usage: bash scripts/security_scan.sh [--quick|--no-history]

Runs Semgrep and Gitleaks security checks.

Options:
  --quick, --no-history  Skip informational Gitleaks history scan.
  --full, --history      Run the informational Gitleaks history scan.
  -h, --help             Show this help.

Environment:
  SECURITY_SCAN_HISTORY=0  Skip history scan.
  SECURITY_REPORT_DIR=...  Override report directory.
  SEMGREP_BIN=...          Use a specific Semgrep executable.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --quick|--no-history)
      RUN_HISTORY=0
      ;;
    --full|--history)
      RUN_HISTORY=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

cd "$ROOT_DIR"
mkdir -p "$REPORT_DIR"

resolve_semgrep() {
  if [[ -n "${SEMGREP_BIN:-}" && -x "${SEMGREP_BIN:-}" ]]; then
    printf '%s\n' "$SEMGREP_BIN"
    return
  fi
  if command -v semgrep >/dev/null 2>&1; then
    command -v semgrep
    return
  fi
  if [[ -x "$SEMGREP_VENV_BIN" ]]; then
    printf '%s\n' "$SEMGREP_VENV_BIN"
    return
  fi
  printf 'semgrep not found. Install it with Homebrew or set SEMGREP_BIN.\n' >&2
  return 1
}

if ! command -v gitleaks >/dev/null 2>&1; then
  printf 'gitleaks not found. Install it with Homebrew before scanning.\n' >&2
  exit 1
fi

SEMGREP="$(resolve_semgrep)"
CURRENT_TREE="$(mktemp -d "$REPORT_DIR/current-tree.XXXXXX")"

cleanup() {
  rm -rf "$CURRENT_TREE"
}
trap cleanup EXIT

printf 'Running Semgrep...\n'
"$SEMGREP" scan \
  --config "$ROOT_DIR/.semgrep.yml" \
  --error \
  --json-output "$REPORT_DIR/semgrep.json" \
  --metrics=off \
  --disable-version-check \
  application domain infrastructure interfaces gateway scripts

printf 'Preparing tracked-file snapshot for Gitleaks current-tree scan...\n'
git ls-files -z | while IFS= read -r -d '' path; do
  mkdir -p "$CURRENT_TREE/$(dirname "$path")"
  cp "$path" "$CURRENT_TREE/$path"
done

printf 'Running Gitleaks on tracked current tree...\n'
gitleaks detect \
  --no-git \
  --source "$CURRENT_TREE" \
  --config "$ROOT_DIR/.gitleaks.toml" \
  --redact \
  --report-format json \
  --report-path "$REPORT_DIR/gitleaks-current.json" \
  --exit-code 1

if [[ "$RUN_HISTORY" == "0" ]]; then
  printf 'Skipping informational Gitleaks history scan.\n'
else
  printf 'Running informational Gitleaks history scan...\n'
  gitleaks detect \
    --source "$ROOT_DIR" \
    --config "$ROOT_DIR/.gitleaks.toml" \
    --redact \
    --report-format json \
    --report-path "$REPORT_DIR/gitleaks-history.json" \
    --exit-code 0
fi

printf 'Security scan reports written to %s\n' "$REPORT_DIR"
