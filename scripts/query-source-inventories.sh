#!/usr/bin/env bash
# Writes every source inventory discovery reads into RUNNER_TEMP, plus the matching
# `ipsw-diff-catalog discover` arguments in RUNNER_TEMP/discovery-args, one per line.
# Pallas is Apple's live OTA service; a failed Pallas query only loses the head start
# over AppleDB, so it warns instead of failing.
set -euo pipefail

: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
: "${APPLEDB_PLATFORM:?APPLEDB_PLATFORM is required}"
: "${DEVICE:?DEVICE is required}"
: "${MAJOR_VERSION:?MAJOR_VERSION is required}"
: "${OTA_FALLBACK:?OTA_FALLBACK is required}"

config="$RUNNER_TEMP/ipsw-config/config.yaml"
arguments="$RUNNER_TEMP/discovery-args"
mkdir -p "$(dirname "$config")"
touch "$config"
: > "$arguments"

query_appledb() {
  local kind="$1"
  local output="$RUNNER_TEMP/$kind-sources.json"
  ipsw --config "$config" dl appledb \
    --os "$APPLEDB_PLATFORM" \
    --device "$DEVICE" \
    --version "$MAJOR_VERSION." \
    --type "$kind" \
    --json \
    --no-color > "$output"
  if ! jq --exit-status '
    type == "object" and .schema_version == 1 and
    (.releases | type == "array" and length > 0)
  ' "$output" >/dev/null; then
    echo "Expected nonempty ipsw AppleDB schema_version=1 $kind releases envelope" >&2
    exit 1
  fi
  printf -- '--%s-sources\n%s\n' "$kind" "$output" >> "$arguments"
}

query_pallas() {
  local audience="$1"
  local output="$RUNNER_TEMP/pallas-$audience.json"
  local platform
  # macOS ships bash 3.2, where an empty array trips set -u unless expanded this way.
  local flags=()
  platform="$(printf '%s' "$APPLEDB_PLATFORM" | tr '[:upper:]' '[:lower:]')"
  if [ "$audience" != release ]; then
    flags+=("--$audience")
  fi
  if perl -e 'alarm shift; exec @ARGV or die "exec: $!"' 180 \
    ipsw --config "$config" dl ota \
    --platform "$platform" \
    --device "$DEVICE" \
    ${flags[@]+"${flags[@]}"} \
    --json \
    --no-color > "$output" &&
    jq --exit-status 'type == "object" and .schema_version == 1 and (.otas | type == "array")' \
      "$output" >/dev/null; then
    printf -- '--pallas-sources\n%s\n' "$output" >> "$arguments"
  else
    echo "::warning::Pallas $audience query failed; discovery continues from AppleDB only"
    rm -f "$output"
  fi
}

query_appledb ipsw
appledb_commit="$(git -C "$RUNNER_TEMP/ipsw-config/appledb" rev-parse HEAD)"
[[ "$appledb_commit" =~ ^[0-9a-f]{40}$ ]]
printf '%s\n' "$appledb_commit" > "$RUNNER_TEMP/appledb-commit"

if [ "$OTA_FALLBACK" = true ]; then
  query_appledb ota
  for audience in release rc beta; do
    query_pallas "$audience"
  done
fi
