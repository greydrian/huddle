# Vendored front-end libraries

Committed as-is and loaded by `<script>`/`<link>` tags: no npm, no build step
(spec 9.1). Dependabot cannot see these, so upgrades are manual and this file
is the record of what is here and where it came from.

| Library | Version | Files | Source (the npm package, or the GitHub release) | Licence |
|---|---|---|---|---|
| HTMX | 2.0.10 | `htmx/htmx.min.js` | `https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.10.tgz` → `package/dist/htmx.min.js`; also `https://github.com/bigskysoftware/htmx/releases/download/v2.0.10/htmx.min.js` | 0BSD (`htmx/LICENSE`) |
| Gridstack | 13.2.0 | `gridstack/gridstack-all.js`, `gridstack/gridstack.min.css` | `https://registry.npmjs.org/gridstack/-/gridstack-13.2.0.tgz` → `package/dist/` | MIT (`gridstack/LICENSE`, `gridstack/gridstack-all.js.LICENSE.txt`) |
| simple-keyboard | 3.8.192 | `simple-keyboard/index.modern.js`, `simple-keyboard/index.css` | `https://registry.npmjs.org/simple-keyboard/-/simple-keyboard-3.8.192.tgz` → `package/build/index.modern.js`, `package/build/css/index.css` | MIT (`simple-keyboard/LICENSE`) |

`SHA256SUMS` holds the hash of every library file. CI's `static` job checks
them (`sha256sum --check --strict SHA256SUMS`) and fails if a file changed or a
new file isn't listed, so an accidental edit to a minified file can't slip
through review. On 30 Sep 2026 every file was confirmed byte-for-byte identical
to the package or release above.

## Upgrading one

1. Download the package tarball from the URL in the table (with the new
   version), unpack it, and copy the same files over the ones here. Copy its
   `LICENSE` too if it changed.
2. Regenerate the manifest from this folder:
   `sha256sum */*.js */*.css > SHA256SUMS` (paths must stay relative to here).
3. Update the version in the table above and in the README's project structure.
4. Run the browser tests (`python -m pytest -m e2e`): drag vs scroll, the
   on-screen keyboard and widget swaps are what these libraries do.
