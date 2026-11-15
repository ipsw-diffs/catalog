#!/usr/bin/env bash
# Runs shard discovery every POLL_INTERVAL seconds until POLL_DURATION has passed, and
# dispatches the shard's discovery workflow once per new edge destination. GitHub runs
# scheduled workflows hours late, so this loop is what makes new builds show up within
# minutes. Expects the shard checkout in ./shard and the catalog checkout in ./catalog.
set -euo pipefail

: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${POLICY_PATH:?POLICY_PATH is required}"
: "${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
: "${TRACK_ID:?TRACK_ID is required}"
: "${DISCOVER_WORKFLOW:?DISCOVER_WORKFLOW is required}"
: "${POLL_INTERVAL:?POLL_INTERVAL is required}"
: "${POLL_DURATION:?POLL_DURATION is required}"

dispatched=" "

refresh_shard() {
  git -C shard fetch --quiet --depth 1 origin main &&
    git -C shard checkout --quiet --detach FETCH_HEAD
}

discover() {
  local inventories=() argument
  while IFS= read -r argument; do
    inventories+=("$argument")
  done < "$RUNNER_TEMP/discovery-args"
  uv run --directory catalog --frozen ipsw-diff-catalog discover \
    --policy "../shard/$POLICY_PATH" \
    --manifests-dir "../shard/$MANIFESTS_DIR" \
    --appledb-repo "$RUNNER_TEMP/ipsw-config/appledb" \
    --appledb-commit "$(cat "$RUNNER_TEMP/appledb-commit")" \
    "${inventories[@]}"
}

generation_pending() {
  local build="$1" paused
  paused="$(gh issue list --repo "$GITHUB_REPOSITORY" --label generation-failed \
    --state open --json number --jq length)" || return 0
  [ "$paused" != 0 ] && return 0
  git -C shard ls-remote --exit-code --heads origin "refs/heads/generated/$TRACK_ID-$build" \
    >/dev/null 2>&1
}

poll_once() {
  local decision build
  refresh_shard || { echo "::warning::could not refresh the shard checkout"; return; }
  bash catalog/scripts/query-source-inventories.sh ||
    { echo "::warning::source inventory query failed"; return; }
  decision="$(discover)" || { echo "::warning::discovery failed"; return; }
  if [ "$(jq --raw-output .status <<< "$decision")" != candidate ]; then
    return
  fi
  build="$(jq --exit-status --raw-output '.edges[0].to.build' <<< "$decision")" || return 0
  if [[ "$dispatched" == *" $build "* ]] || generation_pending "$build"; then
    return
  fi
  if gh workflow run "$DISCOVER_WORKFLOW" --repo "$GITHUB_REPOSITORY" --ref main; then
    dispatched="$dispatched$build "
    echo "::notice::dispatched $DISCOVER_WORKFLOW for $TRACK_ID build $build"
  else
    echo "::warning::could not dispatch $DISCOVER_WORKFLOW for build $build"
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
