#!/usr/bin/env bash
# Asks Apple's Pallas service about the devices of catalog entries that have a build the
# release registry does not label yet, so release-metadata can label builds AppleDB has not
# recorded. Writes the extra release-metadata arguments to RUNNER_TEMP/release-metadata-args,
# one per line. A failed query only means waiting for AppleDB, so it warns instead of failing.
set -euo pipefail

: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
entries_dir="${ENTRIES_DIR:-entries}"
registry="${REGISTRY:-metadata/releases.json}"
arguments="$RUNNER_TEMP/release-metadata-args"
output_dir="$RUNNER_TEMP/pallas-labels"
config="$output_dir/config.yaml"
mkdir -p "$output_dir"
touch "$config"
printf -- '--observed-on\n%s\n' "$(date -u +%F)" > "$arguments"

# ipsw has no iPadOS platform; iPads are queried as iOS devices.
devices="$(jq --raw-output --null-input --slurpfile registry "$registry" '
  ($registry[0].releases
    | map({key: "\(.platform) \(.build)", value: true}) | from_entries) as $known
  | inputs
  | select(($known["\(.platform) \(.from.build)"] and $known["\(.platform) \(.to.build)"]) | not)
  | [(.platform | ascii_downcase), .device] | @tsv
' "$entries_dir"/*.json | sort -u)"

while IFS=$'\t' read -r platform device; do
  [ -n "$device" ] || continue
  for audience in release rc beta; do
    output="$output_dir/$device-$audience.json"
    flags=()
    if [ "$audience" != release ]; then
      flags+=("--$audience")
    fi
    if perl -e 'alarm shift; exec @ARGV or die "exec: $!"' 180 \
      ipsw --config "$config" dl ota \
      --platform "$platform" \
      --device "$device" \
      ${flags[@]+"${flags[@]}"} \
      --json \
      --no-color > "$output" &&
      jq --exit-status 'type == "object" and .schema_version == 1 and (.otas | type == "array")' \
        "$output" >/dev/null; then
      printf -- '--pallas-sources\n%s\n' "$output" >> "$arguments"
    else
      echo "::warning::Pallas $audience query for $device failed; its labels wait for AppleDB"
      rm -f "$output"
    fi
  done
done <<< "$devices"
