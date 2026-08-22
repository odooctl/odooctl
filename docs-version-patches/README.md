# Historical documentation backports

Files in this directory are explicit, reviewable corrections applied by
`scripts/build_versioned_docs.py` after it checks out an immutable release tag.
They do not alter a tag or silently rebuild it from `master`.

Each subdirectory name is the published package version. A `replacements.yml`
file applies exact-match text corrections and fails the build unless each old
fragment occurs once. Other relative file paths replace matching files under
that release's staged `docs/` tree. Exact replacements are preferred because
they preserve unrelated tagged content. Add a backport only for an
operator-impacting error, record why beside it, and prefer a new patch release
where practical.
