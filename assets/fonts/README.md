# PT Root UI Font

This app uses **PT Root UI** as the main interface font.

## How to install the font

1. Go to: https://www.paratype.com/fonts/pt/pt-root-ui
2. Download the font package (free for most uses).
3. Extract the archive. Inside you will find `.woff2` files with names like:
   - `pt-root-ui_regular.woff2`
   - `pt-root-ui_medium.woff2`
   - `pt-root-ui_bold.woff2`

4. Copy the three files above directly into this `assets/fonts/` folder.

   The current CSS expects exactly these filenames (lowercase + underscore):
   - `pt-root-ui_regular.woff2`
   - `pt-root-ui_medium.woff2`
   - `pt-root-ui_bold.woff2`

If the files are missing or have different names, the app will fall back to system fonts.

If the files are missing, the app will gracefully fall back to system sans-serif fonts.

## Font weights used in the app

- Regular (400)
- Medium (500)
- Bold (700)
