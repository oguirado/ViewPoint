#!/bin/bash
set -euo pipefail

surface=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(realpath -- "$surface/../../../..")
base=$(git -C "$repo" rev-parse --verify --end-of-options "${1:?pass a baseline commit}^{commit}")
mkdir -p -- "$surface/build"
run_dir=$(mktemp -d "$surface/build/perf3.XXXXXX")
inputs=$(mktemp -d "$run_dir/inputs.XXXXXX")
trap 'rm -r -- "$inputs"' EXIT

mkdir -- "$inputs/a" "$inputs/b"
git -C "$repo" archive "$base" -- \
    components/vp-direct/schemas \
    components/vp-direct/admission \
    components/vp-direct/data/frames \
    components/vp-direct/perception/baseline \
    components/vp-direct/perception/stream \
    components/vp-direct/perception/pool \
    components/vision-engine/vadapt \
    components/vision-engine/visioncore \
    components/vision-engine/vnative \
    components/vision-engine/eval \
    components/wender-agent/wender | tar -x -C "$inputs/a"
cp -a -- "$inputs/a/." "$inputs/b/"
cp -- "$repo/components/vp-direct/perception/pool/pool.py" \
    "$inputs/b/components/vp-direct/perception/pool/pool.py"
if cmp -s "$inputs/a/components/vp-direct/perception/pool/pool.py" \
          "$inputs/b/components/vp-direct/perception/pool/pool.py"; then
    printf '%s\n' 'baseline and candidate pool.py are identical' >&2
    exit 1
fi

for label in a1 b1 b2 a2; do
    if pgrep -f '[/]accept/t[12]/(LIVE_|benchmark)' > /dev/null; then
        printf '%s\n' 'qualification process overlaps the benchmark window' >&2
        exit 1
    fi
    input=${label%?}
    arm=baseline
    if [[ $input == b ]]; then
        arm=candidate
    fi
    sh "$surface/tests/safe_run.sh" "$inputs/$input/components/vp-direct" \
        --base "$base" --arm "$arm" --samples 12 > "$run_dir/abba-$label.json"
done
printf '%s\n' "$run_dir"
