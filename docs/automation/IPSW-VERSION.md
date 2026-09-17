# ipsw release selection

Discovery and generation install the latest stable `blacktop/ipsw` GitHub release
at the start of each job using `scripts/install-latest-ipsw.sh`. Drafts and
prereleases are excluded. Each job resolves the release once and uses its exact
versioned Linux x86_64 archive for the remainder of the job.

The installer requires the release asset's SHA-256 digest, verifies the archive
before extraction, and checks the executable's reported version. An API error,
missing asset or digest, checksum failure, or version mismatch fails the job;
there is no fallback to an older binary. This trusts releases published by
`blacktop/ipsw`; the digest protects download integrity, not against a compromised
upstream publisher.

Logs contain the resolved version and checksum. Generated provenance retains the
actual `ipsw version` output, including its build commit. Existing reports are not
regenerated merely because a new tool version becomes available.

## Rollout

The shard workflows pin these reusable catalog workflows to a commit SHA.
After publishing a catalog commit containing this installer, update both
`discover.yml@<SHA>` and `generate.yml@<SHA>` references in each shard's
`.github/workflows/discover.yml` to that catalog commit. Updating catalog `main`
alone does not update pinned callers. Keep the workflow SHA pinned; the binary
release is selected dynamically by the installer at job startup.

Start a fresh dispatch after updating callers. Confirm the installation log shows
the latest stable release and that the generated provenance reports that version.
Rerunning an old workflow run uses its original workflow revision.
