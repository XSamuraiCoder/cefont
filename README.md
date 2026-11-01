# CEfont Web

CEfont Web is a static, browser-only TTF/OTF-to-AppVar converter. It uses a
locally bundled FreeType WebAssembly build for hinted monochrome glyphs and
creates the `.8xv` file in the browser. Font files are never uploaded, and
conversion does not require Python, a backend, a package install, or a CDN.

Open `index.html` directly or publish this directory on any static host. Keep
the `vendor` directory alongside the HTML, CSS, and JavaScript files.

The match report compares each generated bitmap against FreeType's
antialiased rendering of the original font outlines. See
`vendor/THIRD_PARTY_NOTICES.md` for the FreeType, lv_font_conv, Emscripten,
zlib, and bundled-font license details.
