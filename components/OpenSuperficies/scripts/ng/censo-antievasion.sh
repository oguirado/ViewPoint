#!/usr/bin/env bash
# SCRIPT_HEADER:BEGIN
# {"id":"srf-ng-censo-antievasion","version":"1.0.0","tipo":"obrero","plazo_ms":10000,"plan_id":"opensuperficies-nextgen","fase":"FASE-0"}
# SCRIPT_HEADER:END
#
# Anti-evasion census scanner for executable trees (INV-6 mechanisation).
# Ensures zero anti-evasion tooling across executable trees and extra paths.
# Markdown files are excluded by extension from scanning.
# The pattern is stored obfuscated via runtime character byte derivation.

set -euo pipefail
export LC_ALL=C

# Obfuscated token derivation via hex byte sequences to avoid self-matching
# and ensure literal four-token alternation never appears in the tree.
T1=$(printf '%b' '\x73\x74\x65\x61\x6c\x74\x68')
T2=$(printf '%b' '\x77\x65\x62\x64\x72\x69\x76\x65\x72')
T3=$(printf '%b' '\x66\x69\x6e\x67\x65\x72\x70\x72\x69\x6e\x74')
T4=$(printf '%b' '\x75\x6e\x64\x65\x74\x65\x63\x74\x65\x64')

# De-obfuscated alternation regex pattern
PATTERN="${T1}|${T2}|${T3}|${T4}"

# Literal patterns to test for literal presence in tree
LIT1="${T1}|${T2}|${T3}|${T4}"
LIT2="${T1}\|${T2}\|${T3}\|${T4}"

json_mode=false
extra_paths=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json)
      json_mode=true
      shift
      ;;
    --extra)
      if [[ $# -lt 2 ]]; then
        echo "uso: $0 [--json] [--extra <path>]" >&2
        exit 2
      fi
      extra_paths+=("$2")
      shift 2
      ;;
    --extra=*)
      extra_paths+=("${1#*=}")
      shift
      ;;
    -h|--help)
      echo "uso: $0 [--json] [--extra <path>]"
      exit 0
      ;;
    *)
      echo "argumento desconocido: $1" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

CANDIDATE_TREES=("src" "cmd" "cockpit" "scripts" "docker" "motor")

files_to_scan=()
ficheros_md_omitidos=0

for tree in "${CANDIDATE_TREES[@]}"; do
  tree_path="$REPO_ROOT/$tree"
  [[ -d "$tree_path" ]] || continue

  while IFS= read -r -d '' f; do
    case "$f" in
      *.md|*.MD|*.markdown|*.mdown|*.mkd)
        # Excluded by extension
        ficheros_md_omitidos=$((ficheros_md_omitidos + 1))
        continue
        ;;
      *)
        files_to_scan+=("$f")
        ;;
    esac
  done < <(find "$tree_path" \
    \( -type d \( -name node_modules -o -name dist -o -name wailsjs -o -name .git -o -path '*/build/bin' \) -prune \) \
    -o -type f -print0)
done

for ep in "${extra_paths[@]}"; do
  if [[ -f "$ep" ]]; then
    case "$ep" in
      *.md|*.MD|*.markdown|*.mdown|*.mkd)
        # Excluded by extension
        ficheros_md_omitidos=$((ficheros_md_omitidos + 1))
        ;;
      *)
        files_to_scan+=("$ep")
        ;;
    esac
  elif [[ -d "$ep" ]]; then
    while IFS= read -r -d '' f; do
      case "$f" in
        *.md|*.MD|*.markdown|*.mdown|*.mkd)
          # Excluded by extension
          ficheros_md_omitidos=$((ficheros_md_omitidos + 1))
          ;;
        *)
          files_to_scan+=("$f")
          ;;
      esac
    done < <(find "$ep" \
      \( -type d \( -name node_modules -o -name dist -o -name wailsjs -o -name .git -o -path '*/build/bin' \) -prune \) \
      -o -type f -print0)
  else
    echo "error: la ruta especificada en --extra no existe o no es valida: $ep" >&2
    exit 2
  fi
done

# Deduplicate files
if [[ ${#files_to_scan[@]} -gt 0 ]]; then
  readarray -t files_to_scan < <(printf '%s\n' "${files_to_scan[@]}" | sort -u)
fi

ficheros_escaneados=${#files_to_scan[@]}
ficheros_md_escaneados=0
for f in "${files_to_scan[@]}"; do
  case "$f" in
    *.md|*.MD|*.markdown|*.mdown|*.mkd)
      ficheros_md_escaneados=$((ficheros_md_escaneados + 1))
      ;;
  esac
done

ocurrencias=0
patron_literal_en_arbol=0

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

run_grep_matcher() {
  local grep_err_file="$TMP_DIR/grep_err.$BASHPID"
  local rc=0
  local out
  out=$(grep "$@" 2>"$grep_err_file") || rc=$?
  if [[ $rc -ge 2 ]]; then
    echo "error: matcher grep fallo con rc=$rc:" >&2
    cat "$grep_err_file" >&2
    exit "$rc"
  fi
  printf '%s' "$out"
}

if [[ $ficheros_escaneados -gt 0 ]]; then
  # INV-6 semantics: count of files containing any forbidden token (case-insensitive, ignoring binaries)
  matched_files=$(run_grep_matcher -I -i -l -E "$PATTERN" "${files_to_scan[@]}")
  if [[ -n "$matched_files" ]]; then
    readarray -t matched_arr < <(printf '%s\n' "$matched_files")
    ocurrencias=${#matched_arr[@]}
  fi

  # Count files containing the literal de-obfuscated pattern string
  literal_matches=$(run_grep_matcher -I -i -l -F -e "$LIT1" -e "$LIT2" "${files_to_scan[@]}")
  if [[ -n "$literal_matches" ]]; then
    readarray -t lit_arr < <(printf '%s\n' "$literal_matches")
    patron_literal_en_arbol=${#lit_arr[@]}
  fi
fi

if [[ "$json_mode" == true ]]; then
  printf '{"ocurrencias":%d,"ficheros_escaneados":%d,"ficheros_md_escaneados":%d,"ficheros_md_omitidos":%d,"patron_literal_en_arbol":%d}\n' \
    "$ocurrencias" "$ficheros_escaneados" "$ficheros_md_escaneados" "$ficheros_md_omitidos" "$patron_literal_en_arbol"
else
  printf 'Censo anti-evasion: ocurrencias=%d ficheros_escaneados=%d ficheros_md_escaneados=%d ficheros_md_omitidos=%d patron_literal_en_arbol=%d\n' \
    "$ocurrencias" "$ficheros_escaneados" "$ficheros_md_escaneados" "$ficheros_md_omitidos" "$patron_literal_en_arbol"
fi

exit 0
