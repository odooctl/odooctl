# Historical documentation backports

Files in this directory are explicit, reviewable corrections applied by
`scripts/build_versioned_docs.py` after it checks out an immutable release tag.
They do not alter a tag or silently rebuild it from `master`.

Each subdirectory name is the published package version; its relative file
paths replace the matching files under that release's `docs/` tree. Add a
backport only when an old snapshot contains an operator-impacting error, and
record why in the file itself. Prefer a new patch release where practical.
