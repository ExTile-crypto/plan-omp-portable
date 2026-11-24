#!/usr/bin/env bash
# Build the Implementer panel for one IntelliJ installation.
#
#   IDE=/path/to/idea ./plugin/build.sh
#
# Set JDK if the IDE's bundled runtime has no javac.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IDE="${IDE:-}"
if [ -z "$IDE" ] || [ ! -f "$IDE/product-info.json" ]; then
  echo "Set IDE to your IntelliJ folder, the one containing product-info.json." >&2
  exit 2
fi
JDK="${JDK:-$IDE/jbr}"
if [ ! -x "$JDK/bin/javac" ]; then
  echo "No javac in $JDK. Set JDK to a JDK matching this IDE's runtime." >&2
  exit 2
fi

NAME="plan-omp-implementer"
VERSION="0.1.1"
BUILD="$ROOT/build"
PACKAGE="$BUILD/package/$NAME/lib"
NUMBER="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["buildNumber"])' "$IDE/product-info.json")"
BRANCH="${NUMBER%%.*}"

rm -rf "$BUILD"
mkdir -p "$BUILD/classes" "$PACKAGE" "$ROOT/dist"
SEARCH=("$IDE/lib")
if [ -d "$IDE/plugins/jcef-plugin/lib" ]; then
  SEARCH+=("$IDE/plugins/jcef-plugin/lib")
fi
CLASSPATH="$(find "${SEARCH[@]}" -name '*.jar' | tr '\n' ':')"
"$JDK/bin/javac" -nowarn -proc:none -cp "$CLASSPATH" -d "$BUILD/classes" "$ROOT"/src/main/java/local/planomp/*.java
cp -R "$ROOT"/src/main/resources/. "$BUILD/classes/"
python3 -I - "$BUILD/classes/META-INF/plugin.xml" "$BRANCH" <<'PY'
import re
import sys
from pathlib import Path

path, branch = Path(sys.argv[1]), sys.argv[2]
text, count = re.subn(r'<idea-version [^>]*/>', f'<idea-version since-build="{branch}" until-build="{branch}.*"/>', path.read_text())
if count != 1:
    sys.exit("plugin.xml has no idea-version")
path.write_text(text)
PY
(cd "$BUILD/classes" && python3 -I -m zipfile -c "$PACKAGE/$NAME.jar" META-INF implementer local)
rm -f "$ROOT/dist/$NAME-$VERSION.zip"
(cd "$BUILD/package" && python3 -I -m zipfile -c "$ROOT/dist/$NAME-$VERSION.zip" "$NAME")
echo "$ROOT/dist/$NAME-$VERSION.zip"
