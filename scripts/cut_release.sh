#!/usr/bin/env bash
# Cut a full-database release and refresh the archive the repository ships,
# from one database, in one pass.
#
#   scripts/cut_release.sh              # build and check everything, publish nothing
#   scripts/cut_release.sh --publish    # ... and upload the release
#
# Both halves belong in one command because they have to describe the same
# instant. Cut by hand on 2026-08-22 they described instants two days apart, and
# nothing in the repository could say so. Here the database is hashed before and
# after, and a cut that straddles a `daily_update.sh` run aborts instead of
# publishing a release whose notes are quietly wrong.
#
# Order matters when publishing: `gh release create` tags the current tip of the
# default branch, so the refreshed archive has to be committed and pushed first
# or the tag's source tree ships the previous one. The script checks that rather
# than trusting anyone to remember it.
set -euo pipefail

DB=data/kilauea.db
CORE=data/kilauea_core.db
REPO=RTCK-reina/kilauea-eruption-database
TAG=""
WORK=""
PUBLISH=0
PY="${PYTHON:-python3}"

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --publish)  PUBLISH=1 ;;
        --db)       DB="$2";   shift ;;
        --core)     CORE="$2"; shift ;;
        --repo)     REPO="$2"; shift ;;
        --tag)      TAG="$2";  shift ;;
        --work)     WORK="$2"; shift ;;
        -h|--help)  usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
    shift
done

cd "$(dirname "$0")/.."
TAG="${TAG:-db-$(date -u +%Y-%m-%d)}"
if [ -n "$WORK" ]; then
    OURS=0                # a directory the caller named is the caller's to keep
    mkdir -p "$WORK"
else
    OURS=1
    WORK=$(mktemp -d -t kilauea-release)
fi
ARCHIVE="$CORE.gz"

step() { printf '\n== %s\n' "$*"; }
die()  { printf 'cut_release: %s\n' "$*" >&2; exit 1; }

step "preflight"
[ -f "$DB" ] || die "no database at $DB"
for tool in gzip split shasum "$PY"; do
    command -v "$tool" >/dev/null || die "$tool is not on PATH"
done
if [ "$PUBLISH" = 1 ]; then
    command -v gh >/dev/null || die "gh is not installed; cannot publish"
    gh auth status >/dev/null 2>&1 || die "gh is not authenticated; run 'gh auth login'"
    if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
        die "release $TAG already exists; pass --tag or delete it first"
    fi
fi
printf '  database %s\n  tag      %s\n  repo     %s\n  workdir  %s\n' \
       "$DB" "$TAG" "$REPO" "$WORK"

step "checkpoint and fingerprint the database"
sqlite3 "$DB" 'PRAGMA wal_checkpoint(TRUNCATE);' > /dev/null
SHA_BEFORE=$(shasum -a 256 "$DB" | awk '{print $1}')
printf '  sha256 %s\n' "$SHA_BEFORE"

step "derive the core database and refresh the committed archive"
"$PY" -m kilauea core-db --db "$DB" -o "$CORE" --force
# -n: no name or timestamp in the gzip header, so an unchanged database
# compresses to identical bytes and git records nothing.
gzip -9 -n -c "$CORE" > "$ARCHIVE.new"
gzip -t "$ARCHIVE.new"
gunzip -c "$ARCHIVE.new" | cmp -s - "$CORE" || die "the archive does not match $CORE"
if [ -f "$ARCHIVE" ] && cmp -s "$ARCHIVE.new" "$ARCHIVE"; then
    rm -f "$ARCHIVE.new"
    ARCHIVE_CHANGED=0
    printf '  %s is unchanged\n' "$ARCHIVE"
else
    mv "$ARCHIVE.new" "$ARCHIVE"
    ARCHIVE_CHANGED=1
    printf '  %s rewritten (%s)\n' "$ARCHIVE" "$(du -h "$ARCHIVE" | cut -f1)"
fi

step "compress and split the full database"
gzip -6 -n -c "$DB" > "$WORK/kilauea.db.gz"
gzip -t "$WORK/kilauea.db.gz"
( cd "$WORK" && split -b 500m -d -a 2 kilauea.db.gz kilauea.db.gz.part && rm -f kilauea.db.gz )
( cd "$WORK" && shasum -a 256 kilauea.db.gz.part* > SHA256SUMS.txt )
ls -l "$WORK"/kilauea.db.gz.part* | awk '{printf "  %s  %.0f MB\n", $NF, $5/1048576}'

step "the database did not move while we were reading it"
SHA_AFTER=$(shasum -a 256 "$DB" | awk '{print $1}')
[ "$SHA_BEFORE" = "$SHA_AFTER" ] || die \
    "the database changed mid-cut (a daily_update.sh run?). Nothing was published; re-run."
printf '  unchanged\n'

step "release notes"
"$PY" -m kilauea release-notes --db "$DB" --tag "$TAG" --repo "$REPO" \
      --full-sha "$SHA_BEFORE" --parts-dir "$WORK" --core "$CORE" \
      -o "$WORK/NOTES.md" > /dev/null
printf '  %s (%s bytes)\n' "$WORK/NOTES.md" "$(wc -c < "$WORK/NOTES.md" | tr -d ' ')"

if [ "$PUBLISH" != 1 ]; then
    step "not publishing (--publish was not given)"
    printf '  assets ready in %s\n' "$WORK"
    [ "$ARCHIVE_CHANGED" = 1 ] \
        && printf '  commit the refreshed archive, then re-run with --publish:\n    git add %s && git commit -m "Refresh the core database archive (%s)" && git push\n' "$ARCHIVE" "$TAG" \
        || printf '  the committed archive already matches; re-run with --publish to upload\n'
    exit 0
fi

step "the tag must point at a commit that carries this archive"
git diff --quiet HEAD -- "$ARCHIVE" || die \
    "$ARCHIVE differs from HEAD. Commit and push it first, or the tag ships the previous archive."
UPSTREAM=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null) || die "no upstream branch"
git fetch -q origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse "$UPSTREAM")" ] || die \
    "HEAD and $UPSTREAM differ; push first so the release tags the right commit."
printf '  HEAD %s is pushed and carries the archive\n' "$(git rev-parse --short HEAD)"

step "publish"
gh release create "$TAG" --repo "$REPO" \
   --title "Full database snapshot — ${TAG#db-}" \
   --notes-file "$WORK/NOTES.md" --latest \
   "$WORK"/kilauea.db.gz.part* "$WORK/SHA256SUMS.txt"
gh release view "$TAG" --repo "$REPO" --json assets \
   -q '.assets[] | "  \(.name)  \(.size)  \(.state)"'
if [ "$OURS" = 1 ]; then
    rm -rf "$WORK"
else
    printf '\n  assets and notes left in %s\n' "$WORK"
fi
