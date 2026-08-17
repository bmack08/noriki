"""Build deploy/ — the Noriki PWA, ready to serve from GitHub Pages.

Mirrors the IRONPATH pattern: sources live in static/, this copies and
inlines them into deploy/, which is what Pages actually serves.

    python build_single.py

Also writes dist/Noriki.html — the whole app in one self-contained file,
for when you just want to open it from a download with no hosting at all.
"""

import base64
import hashlib
import os
import re
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
DEPLOY = os.path.join(HERE, "deploy")
DIST = os.path.join(HERE, "dist")

# Files whose content decides the build id. If any of them changes, the
# service worker cache name changes too — otherwise phones keep serving the
# old app forever and pushes appear to do nothing.
SHELL = ["index.html", "style.css", "app.js", "manifest.json"]


def read(name):
    with open(os.path.join(STATIC, name), "r", encoding="utf-8") as f:
        return f.read()


def build_id():
    h = hashlib.sha256()
    for name in SHELL:
        h.update(read(name).encode("utf-8"))
    return h.hexdigest()[:8]


def png_uri(name):
    path = os.path.join(STATIC, name)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


def build_deploy(bid):
    """Copy static/ to deploy/, stamping the build id into the cache name."""
    os.makedirs(DEPLOY, exist_ok=True)
    for name in os.listdir(STATIC):
        src = os.path.join(STATIC, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(DEPLOY, name))

    # Stamp the build id so a push actually reaches installed phones.
    sw = read("sw.js").replace('const CACHE = "noriki-v1";',
                               f'const CACHE = "noriki-{bid}";')
    with open(os.path.join(DEPLOY, "sw.js"), "w", encoding="utf-8") as f:
        f.write(sw)

    app = read("app.js").replace('const BUILD = "v1";', f'const BUILD = "{bid}";')
    with open(os.path.join(DEPLOY, "app.js"), "w", encoding="utf-8") as f:
        f.write(app)

    # GitHub Pages runs everything through Jekyll unless told not to
    with open(os.path.join(DEPLOY, ".nojekyll"), "w", encoding="utf-8"):
        pass

    total = sum(
        os.path.getsize(os.path.join(DEPLOY, f))
        for f in os.listdir(DEPLOY)
        if os.path.isfile(os.path.join(DEPLOY, f))
    )
    print(f"built {DEPLOY} ({total / 1024:.0f} KB) build={bid}")


def build_single(bid):
    """Inline everything into one portable file — no hosting required."""
    html = read("index.html")
    html = html.replace('<link rel="stylesheet" href="style.css">',
                        "<style>\n" + read("style.css") + "\n</style>")
    app = read("app.js").replace('const BUILD = "v1";', f'const BUILD = "{bid}";')
    html = html.replace('<script src="app.js"></script>',
                        "<script>\n" + app + "\n</script>")
    html = html.replace('<link rel="manifest" href="manifest.json">', "")

    icon = png_uri("icon-192.png")
    if icon:
        html = html.replace('<link rel="icon" href="icon-192.png">',
                            f'<link rel="icon" href="{icon}">')

    # no service worker when there's no origin to scope it to
    html = re.sub(r'<script>\s*if \("serviceWorker".*?</script>', "", html, flags=re.S)

    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, "Noriki.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"built {out} ({os.path.getsize(out) / 1024:.0f} KB)")


if __name__ == "__main__":
    _bid = build_id()
    build_deploy(_bid)
    build_single(_bid)
