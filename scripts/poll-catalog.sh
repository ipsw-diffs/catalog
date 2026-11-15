#!/usr/bin/env bash
# Dispatches catalog-publish.yml whenever a shard's main branch moves, and every
# REPUBLISH_INTERVAL seconds so a publication waiting for AppleDB is retried, until
# POLL_DURATION has passed. GitHub runs scheduled workflows hours late, so this loop is what
# carries a merged shard diff to the catalog and the social publisher within minutes.
set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${POLL_INTERVAL:?POLL_INTERVAL is required}"
: "${POLL_DURATION:?POLL_DURATION is required}"
: "${REPUBLISH_INTERVAL:?REPUBLISH_INTERVAL is required}"

shards="$(jq --exit-status --raw-output '.shards[] | [.id, .repository] | @tsv' \
  automation/shards.json)"
last_heads=""
last_dispatch=$((-REPUBLISH_INTERVAL))

shard_heads() {
  local id repository sha heads=""
  while IFS=$'\t' read -r id repository; do
    sha="$(gh api "repos/${repository#https://github.com/}/commits/main" --jq .sha)" ||
      return 1
    heads="$heads$id=$sha "
  done <<< "$shards"
  printf '%s' "$heads"
}

poll_once() {
  local heads
  heads="$(shard_heads)" || { echo "::warning::could not read every shard head"; return; }
  if [ "$heads" = "$last_heads" ] &&
    [ $((SECONDS - last_dispatch)) -lt "$REPUBLISH_INTERVAL" ]; then
    return
  fi
  if gh workflow run catalog-publish.yml --repo "$GITHUB_REPOSITORY" --ref main; then
    last_heads="$heads"
    last_dispatch=$SECONDS
    echo "::notice::dispatched catalog-publish.yml"
  else
    echo "::warning::could not dispatch catalog-publish.yml"
  fi
}

end=$((SECONDS + POLL_DURATION))
while [ "$SECONDS" -lt "$end" ]; do
  started=$SECONDS
  poll_once
  pause=$((started + POLL_INTERVAL - SECONDS))
  left=$((end - SECONDS))
  if [ "$pause" -gt "$left" ]; then
    pause=$left
  fi
  if [ "$pause" -gt 0 ]; then
    sleep "$pause"
  fi
done
