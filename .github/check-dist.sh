#!/usr/bin/env bash
# usage: .github/check-dist.sh <dist dir> <tag or "">
# Checks a `uv build` output before it is published: the version matches the tag, the
# schema files ship, nothing private ships, and both entry points load.
set -euo pipefail

DIST="$1"
TAG="${2:-}"
fail() { echo "check-dist: $*" >&2; exit 1; }

whl=("$DIST"/*.whl)
sdist=("$DIST"/*.tar.gz)
[ -f "${whl[0]}" ] && [ -f "${sdist[0]}" ] || fail "no wheel or sdist in $DIST. Run uv build first."

version="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
if [ -n "$TAG" ] && [ "$TAG" != "v$version" ]; then
  fail "tag $TAG does not match pyproject version $version. Bump the version or retag as v$version."
fi

wheel_files="$(python3 -m zipfile -l "${whl[0]}")"
sdist_files="$(tar tzf "${sdist[0]}")"
for f in covey/schema/card.json covey/schema/api.yaml; do
  grep -q "$f" <<<"$wheel_files" || fail "$f is missing from the wheel. Check [tool.hatch.build] in pyproject.toml."
  grep -q "$f" <<<"$sdist_files" || fail "$f is missing from the sdist. Check [tool.hatch.build] in pyproject.toml."
done

private='(^|/)(\.claude/|CLAUDE\.md|CLAUDE\.local\.md|\.env|\.mcp\.json)'
if printf "%s\n%s\n" "$wheel_files" "$sdist_files" | grep -E "$private"; then
  fail "private files (above) would be published. Exclude them in pyproject.toml."
fi

uv run --isolated --no-project --with "${whl[0]}" covey --help >/dev/null || fail "covey --help failed from the wheel."
# coveyd has no --help (it would start a daemon), so import its entry point instead.
uv run --isolated --no-project --with "${whl[0]}" python -c "from covey.daemon.app import main" \
  || fail "coveyd's entry point does not import from the wheel."
echo "check-dist: ok (${whl[0]##*/}, ${sdist[0]##*/})"
