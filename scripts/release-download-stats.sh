#!/usr/bin/env bash
# Download tallies from firekeep-dist's GitHub Release assets.
#
#   ./scripts/release-download-stats.sh
#
# Reads the public GitHub API (repos/kapella-hub/firekeep-dist/releases) —
# each asset's download_count is GitHub's own durable counter, immune to the
# blind spots in the site's script-fetch counter (dl-counter.php): it counts
# EVERY file (wheels, uv binaries, checksums), not just the entry script, and
# needs no server-side log at all. Human-run, on demand — nothing schedules
# this and no dashboard reads it, same precedent as
# firekeep-site/scripts/download-stats.sh.
#
# Requires: gh CLI, authenticated with read access to kapella-hub/firekeep-dist
# (public repo — `gh auth status` with no special scope is enough).
set -euo pipefail

gh api repos/kapella-hub/firekeep-dist/releases --paginate --jq '
  .[] | .tag_name as $tag | .assets[] | "\($tag)\t\(.name)\t\(.download_count)"
' | awk -F'\t' '
    { total += $3; bytag[$1] += $3; byfile[$2] += $3; n++ }
    END {
        if (n == 0) { print "no releases found (or FIREKEEP_DIST_RELEASE_TOKEN is not yet configured — see release.yml)"; exit }
        printf "total asset downloads: %d\n\nby release:\n", total
        for (t in bytag)  printf "  %-20s %d\n", t, bytag[t]
        printf "\nby file (summed across releases):\n"
        for (f in byfile) printf "  %-40s %d\n", f, byfile[f]
    }'
