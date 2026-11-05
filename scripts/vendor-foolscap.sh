#!/usr/bin/env bash
# Copy the Foolscap design system (github.com/j4p3/foolscap) into the dashboard's static files.
# The dashboard is served offline, so the stylesheets, fonts, the Plot helper and its Observable Plot / d3
# bundles are vendored rather than loaded from a CDN. Re-run after changing Foolscap; edit Foolscap, not the copies.
#   FOOLSCAP=~/src/foolscap scripts/vendor-foolscap.sh
set -euo pipefail
src="${FOOLSCAP:-$HOME/src/foolscap}"
dst="$(cd "$(dirname "$0")/.." && pwd)/src/civilizer/web/foolscap"
(cd "$src" && npm run -s tokens >/dev/null && npm run -s fonts)
rm -rf "$dst"
mkdir -p "$dst/fonts"
cp "$src/dist/foolscap.vars.css" "$src/site/assets/foolscap.css" "$src/site/assets/foolscap-plot.js" "$dst/"
cp "$src/node_modules/@observablehq/plot/dist/plot.umd.min.js" "$src/node_modules/d3/dist/d3.min.js" "$dst/"
cp "$src/site/assets/fonts/"*.woff2 "$dst/fonts/"
# fonts.css expects to be inlined into site/assets/built/; here it sits next to fonts/
sed 's#url("\.\./fonts/#url("fonts/#g' "$src/site/assets/fonts.css" > "$dst/fonts.css"
echo "foolscap $(git -C "$src" describe --always --dirty) -> $dst"
