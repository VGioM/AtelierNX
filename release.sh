#!/bin/bash
# Publie une nouvelle version sur GitHub, téléchargeable et proposée par la
# mise à jour intégrée (SelfUpdate.swift) aux apps déjà installées.
#
#   ./release.sh 1.3.1                 notes tirées des commits depuis la dernière version
#   ./release.sh 1.3.1 notes.md        notes écrites à la main (la partie « Installation » est ajoutée)
#
# Étapes : version dans build.sh, build + installation, tests s'il y en a,
# commit « Version X.Y.Z », tag vX.Y.Z, push, puis release GitHub avec
# <App>-X.Y.Z-macOS-arm64.zip.
#
# Plusieurs plateformes : une même release porte un fichier par plateforme,
# nommé <App>-X.Y.Z-<plateforme>-<arch>.<ext> (macOS-arm64.zip,
# linux-x86_64.tar.gz, windows-x64.zip…). Chaque app ne prend que le sien ;
# ajoutez les autres avec `gh release upload vX.Y.Z <fichier>`.
set -euo pipefail
cd "$(dirname "$0")"

# ── Réglages propres au dépôt ────────────────────────────────────────────────
NAME="Atelier NX"
REPO="VGioM/AtelierNX"
APP="dist/Atelier NX.app"
ASSET="AtelierNX"                   # préfixe du zip
BUILD="./build.sh install"                # compile et installe dans /Applications
# ─────────────────────────────────────────────────────────────────────────────

VERSION="${1:-}"
NOTES_FILE="${2:-}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "usage : ./release.sh X.Y.Z [notes.md]"; exit 1; }
TAG="v$VERSION"

[ "$(git branch --show-current)" = "main" ] || { echo "✗ Pas sur main."; exit 1; }
git fetch -q origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || [ -z "$(git log HEAD..origin/main --oneline)" ] \
    || { echo "✗ origin/main a des commits absents d'ici : faites un pull d'abord."; exit 1; }
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && { echo "✗ Le tag $TAG existe déjà."; exit 1; }
[ "$(git config user.email)" = "309474935+VGioM@users.noreply.github.com" ] \
    || { echo "✗ git config user.email n'est pas l'adresse noreply (voir CLAUDE.md)."; exit 1; }
if [ -n "$(git status --porcelain)" ]; then
    echo "✗ Des modifications ne sont pas committées :"; git status --short; exit 1
fi
PREVIOUS="$(git describe --tags --abbrev=0 2>/dev/null || true)"

echo "▸ Version $VERSION dans build.sh…"
python3 -I - build.sh "$VERSION" <<'PY'
import re, sys
path, version = sys.argv[1], sys.argv[2]
text = open(path).read()
key = r"(<key>%s</key>\s*<string>)([^<]*)(</string>)"
text, n = re.subn(key % "CFBundleShortVersionString", lambda m: m[1] + version + m[3], text)
if n != 1:
    sys.exit("CFBundleShortVersionString introuvable dans build.sh")
def build_number(m):
    # Un numéro de build entier avance d'un cran ; sinon il suit la version.
    return m[1] + (str(int(m[2]) + 1) if m[2].isdigit() else version) + m[3]
text = re.sub(key % "CFBundleVersion", build_number, text, count=1)
open(path, "w").write(text)
PY

echo "▸ Build…"
$BUILD
if [ -x Tests/run-unit.sh ]; then
    echo "▸ Tests…"
    ./Tests/run-unit.sh >/dev/null || { echo "✗ Les tests échouent."; git checkout build.sh; exit 1; }
fi

# Une version signée ad hoc serait refusée par les apps déjà installées, qui
# n'acceptent que leur propre certificat.
AUTHORITY="$(codesign -dvv "$APP" 2>&1 | sed -n 's/^Authority=//p' | head -1)"
if [ -z "$AUTHORITY" ]; then
    echo "✗ $APP est signée ad hoc : lancez ./signing-cert.sh (ou importez le certificat) puis recommencez."
    git checkout build.sh; exit 1
fi
codesign --verify --deep --strict "$APP"
if LC_ALL=C grep -qaF "$HOME/" "$APP/Contents/MacOS/"*; then
    echo "✗ Le binaire contient encore des chemins de ce Mac."; git checkout build.sh; exit 1
fi

echo "▸ Commit et tag $TAG…"
git add build.sh
git commit -q -m "Version $VERSION" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git tag "$TAG"
BAD="$(git log --format='%ae %ce' origin/main..HEAD | tr ' ' '\n' | grep -v '^309474935+VGioM@users.noreply.github.com$' || true)"
[ -z "$BAD" ] || { echo "✗ Un commit utilise une autre adresse que noreply : $BAD"; exit 1; }
git push -q origin main "$TAG"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ZIP="$WORK/$ASSET-$VERSION-macOS-arm64.zip"
ditto -c -k --keepParent "$APP" "$ZIP"

{
    if [ -n "$NOTES_FILE" ]; then
        cat "$NOTES_FILE"
    else
        echo "## Nouveautés"
        echo
        git log --format='- %s' ${PREVIOUS:+$PREVIOUS..}HEAD~1 | grep -vE '^- (Version |Ajoute CLAUDE\.md)' || true
    fi
    cat <<DOC

## Installation

1. Téléchargez le fichier \`$ASSET-$VERSION-macOS-arm64.zip\` ci-dessous et ouvrez-le.
2. Glissez **$NAME.app** dans le dossier **Applications**.
3. Au premier lancement, macOS bloque l'app car elle n'est pas notarisée par Apple. Ouvrez **Réglages Système → Confidentialité et sécurité**, descendez jusqu'au message concernant $NAME et cliquez sur **Ouvrir quand même**. Ou, dans le Terminal :
   \`\`\`
   xattr -dr com.apple.quarantine "/Applications/$NAME.app"
   \`\`\`

Ensuite, $NAME vérifie elle-même au lancement s'il existe une nouvelle version et propose de l'installer (menu $NAME → Rechercher les mises à jour…).

**Configuration requise :** macOS 14 ou plus récent, Mac Apple Silicon (M1 et suivants).
DOC
} > "$WORK/notes.md"

echo "▸ Release GitHub…"
gh release create "$TAG" -R "$REPO" --title "$NAME $VERSION" --notes-file "$WORK/notes.md" --latest "$ZIP"
echo "✓ $NAME $VERSION publié"
