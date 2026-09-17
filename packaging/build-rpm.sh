#!/usr/bin/env bash
# Builds lightmorphic-flow-<version>-1.noarch.rpm into packaging/out/
set -euo pipefail

command -v rpmbuild >/dev/null || {
  echo "rpmbuild is not installed. On Debian or Ubuntu:  sudo apt install rpm" >&2
  exit 1
}

here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"
name="lightmorphic-flow"
version="$(python3 -c "import sys; sys.path.insert(0,'$root'); import lmflow; print(lmflow.__version__)")"
top="$(mktemp -d)"
out="$here/out"
mkdir -p "$out" "$top"/{SOURCES,SPECS,BUILD,RPMS,SRPMS}

# Source tarball, named the way the spec expects.
stage="$top/$name-$version"
mkdir -p "$stage"
cp -r "$root/lmflow" "$root/tests" "$root/README.md" "$root/LICENSE" "$stage/"
mkdir -p "$stage/packaging"
cp "$here/lmflow.svg" "$stage/packaging/"
find "$stage" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
tar -czf "$top/SOURCES/$name-$version.tar.gz" -C "$top" "$name-$version"

sed "s/^Version:.*/Version:        $version/" "$here/$name.spec" > "$top/SPECS/$name.spec"

rpmbuild --define "_topdir $top" --define "_dbpath $top/rpmdb" -bb "$top/SPECS/$name.spec" >"$top/build.log" 2>&1 || {
  tail -30 "$top/build.log" >&2
  exit 1
}

found="$(find "$top/RPMS" -name '*.rpm' | head -1)"
cp "$found" "$out/"
rm -rf "$top"
echo "$out/$(basename "$found")"
