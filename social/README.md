# ipsw-diff social publisher

Posts each new catalog entry to X, Mastodon and Bluesky with
[xpost](https://github.com/blacktop/xpost). Every post carries a 1600×900 card, the release
names, the largest counted changes from the diff's README, and a link to that README at its
immutable commit.

The [Social publisher](../.github/workflows/social.yml) workflow runs when a merge to `main`
changes `catalog.json`, and hourly to pick up anything left in the queue. A cheap check job
reads the state issue first, so xpost is only built when there's something to post.
Publishing needs no approval: merged catalog entries go out on their own.

## Setup

1. In `ipsw-diffs/catalog`, create a GitHub Actions [environment][environments] named
   `social`. Restrict deployments to the `main` branch and leave required reviewers off.
2. Protect `main`: require pull requests with Code Owner review, dismiss stale approvals,
   and don't allow anyone, administrators included, to bypass the rules or push directly.
   The [CODEOWNERS file](../.github/CODEOWNERS) assigns `social/`, the workflows and
   CODEOWNERS itself to `@blacktop`.

   This is what guards the credentials. Anyone who can land a change on `main` can read
   them, so keep write and admin access to account owners, and rotate a credential if
   an unreviewed change ever ran with it.
3. Add these secrets **only to the `social` environment**, never as repository or
   organization secrets, for the dedicated ipsw-diffs accounts:

   | Secret | Value |
   | --- | --- |
   | `XPOST_TWITTER_USER` | X username |
   | `XPOST_TWITTER_ACCOUNT_ID` | X's numeric id for that account |
   | `XPOST_TWITTER_STATE_B64` | base64 of a session exported with `xpost twitter export-session` |
   | `XPOST_MASTODON_SERVER` | the account's server, e.g. `https://mastodon.social` |
   | `XPOST_MASTODON_ACCESS_TOKEN` | token with `write:statuses` and `write:media` |
   | `XPOST_BLUESKY_HANDLE` | Bluesky handle |
   | `XPOST_BLUESKY_APP_PASSWORD` | Bluesky app password |

   Upload the X session from the Mac that exported it, without printing it:

   ```fish
   base64 -i ~/.config/xpost/x-session.json | gh secret set XPOST_TWITTER_STATE_B64 --repo ipsw-diffs/catalog --env social
   ```

   Set up all three networks before the first publishing run. Missing provider credentials
   can leave that network pending; a missing exported X session stops the workflow first.

4. Record the current catalog so nothing already in it gets posted:

   ```fish
   gh workflow run social.yml --repo ipsw-diffs/catalog -f mode=bootstrap
   ```

   This creates the state issue, "[automation] ipsw-diff social publisher state". Its body is
   public and never holds credentials.

When X ends the session, posts to X fail and stay pending. Export a new session, update
`XPOST_TWITTER_STATE_B64`, then requeue the pending entries.

## How duplicates are avoided

The state issue records, per entry and per network, what is queued, pending or posted. Before
xpost runs, the entry is marked pending on every network it still needs, and that is saved
to the issue. Each network moves to `posts` only after xpost confirms it. A run posts at most
three entries.

A network that fails, times out or dies mid-run stays pending, and pending entries are never
retried on their own: xpost can't always tell whether X published a post it didn't confirm.
The run fails so the error is visible. The other networks still post.

To review a pending entry, check the account, then edit the state issue:

- It posted: move the network from `pending` into that entry's `posts`.
- It didn't: delete the entry from `pending` and add its ID to `queue`. The next run posts
  only to the networks missing from `posts`.

A catalog change that removes an entry ID or changes any existing row stops the publisher
before enqueueing additions or advancing the saved revision. Row and object-key ordering
do not count as changes. The catalog and every entry must declare integer `schema_version: 1`,
including on first-run initialization and explicit bootstrap.

## Try one entry without posting

```fish
gh workflow run social.yml --repo ipsw-diffs/catalog -f mode=dry-run -f entry_id=ios-27.2-24A437-24B5084k
```

The run prints the post, checks it against each network's limits with `xpost --dry-run`, and
keeps the card as the `ipsw-diff-social-preview` artifact for seven days. It passes no account
secrets to xpost.

Locally, with xpost installed:

```fish
uv sync --project social --locked --all-groups
uv run --project social ipsw-diff-social --mode dry-run --xpost (command -s xpost) \
  --entry-id ios-27.2-24A437-24B5084k --output-dir /tmp/ipsw-social
open /tmp/ipsw-social/ios-27.2-24A437-24B5084k.jpg
```

[environments]: https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments
