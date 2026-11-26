# Fonts in the web page

`display/page.html` carries two fonts inside it, as `data:` URLs in its second style block, so the car computer's
own screen (no internet, and a page policy that loads nothing from elsewhere) shows the same type as a phone:

- **Inter** (https://github.com/rsms/inter), weights 400 to 800. Licence: `OFL-Inter.txt`.
- **Bricolage Grotesque** (https://github.com/ateliertriay/bricolage), weights 600 to 800, with its optical size
  axis. Licence: `OFL-Bricolage-Grotesque.txt`.

Both under the SIL Open Font License 1.1. Made from the Fontsource packages `@fontsource-variable/inter` (5.3.0,
`inter-latin-wght-normal.woff2`) and `@fontsource-variable/bricolage-grotesque` (`bricolage-grotesque-latin-opsz-normal.woff2`):

    python -m fontTools.subset IN.woff2 --unicodes="U+0020-007E,U+00A0-00FF,U+2013-2014,U+2018-201D,U+2026,U+2191,U+2192,U+2212,U+2248,U+2713,U+2022" --layout-features+=tnum --flavor=woff2 --output-file=OUT.woff2
    python -m fontTools.varLib.instancer OUT.woff2 wght=400:800 -o inter.woff2              # 600:800 for Bricolage

then base64 into the two `@font-face` rules. A character outside that list falls back to the system font.
