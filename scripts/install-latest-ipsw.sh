#!/usr/bin/env bash
# Resolve once per job and log the selected release and checksum.
set -euo pipefail

: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
: "${GITHUB_ENV:?GITHUB_ENV is required}"
: "${GITHUB_PATH:?GITHUB_PATH is required}"

release="$(gh api repos/blacktop/ipsw/releases/latest)"
tag="$(jq --exit-status --raw-output '
  select(.draft == false and .prerelease == false) |
  .tag_name | strings | select(test("^v[0-9]+\\.[0-9]+\\.[0-9]+$"))
' <<< "$release")"
version="${tag#v}"
asset="ipsw_${version}_linux_x86_64.tar.gz"
digest="$(jq --exit-status --raw-output --arg asset "$asset" '
  [.assets[] | select(.name == $asset and .state == "uploaded")] |
  select(length == 1) | .[0].digest | strings |
  select(test("^sha256:[0-9a-f]{64}$"))
' <<< "$release")"
sha256="${digest#sha256:}"
install_root="$(mktemp -d "$RUNNER_TEMP/ipsw-install.XXXXXX")"
archive="$install_root/$asset"
install_dir="$install_root/bin"
max_attempts=5

printf 'Installing ipsw %s (SHA-256 %s)\n' "$tag" "$sha256"
# Use the job token: anonymous release downloads have failed with 504s during GitHub incidents.
for attempt in $(seq 1 "$max_attempts"); do
  if timeout 300 gh release download "$tag" --repo blacktop/ipsw \
    --pattern "$asset" --output "$archive" --clobber; then
    break
  fi
  if [ "$attempt" -eq "$max_attempts" ]; then
    printf 'failed to download %s after %s attempts\n' "$asset" "$max_attempts" >&2
    exit 1
  fi
  sleep 5
done
printf '%s  %s\n' "$sha256" "$archive" | shasum --algorithm 256 --check --strict
mkdir "$install_dir"
tar -xzf "$archive" -C "$install_dir" ipsw
actual_version="$("$install_dir/ipsw" version)"
case "$actual_version" in
  "Version: $version,"* | "Version: v$version,"*) ;;
  *) printf 'ipsw version mismatch: expected %s, got %s\n' "$tag" "$actual_version" >&2; exit 1 ;;
esac
printf '%s\n' "$actual_version"

# Only expose a verified, successfully executed binary to subsequent steps.
{
  printf 'IPSW_VERSION=%s\n' "$version"
  printf 'IPSW_ARCHIVE_SHA256=%s\n' "$sha256"
} >> "$GITHUB_ENV"
printf '%s\n' "$install_dir" >> "$GITHUB_PATH"
