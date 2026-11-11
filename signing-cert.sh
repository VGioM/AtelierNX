#!/bin/bash
# Creates, once, the local certificate build.sh signs the app with. One
# certificate, "VGioM Local Signing", serves Biblifilm, Atelier NX and Recomps.
# It must stay the same from one release to the next: the in-app updater only
# installs a new version signed with the certificate of the running one.
#
# An ad-hoc signature changes on every build, so macOS takes each new build for
# a different app and asks again for Documents and the other protected
# folders. A certificate that stays the same lets it remember the answer.
set -euo pipefail

NAME="VGioM Local Signing"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if security find-identity -p codesigning "$KEYCHAIN" | grep -q "$NAME"; then
    echo "✓ Le certificat « $NAME » existe déjà."
    exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/cfg" <<CFG
[req]
distinguished_name=dn
x509_extensions=ext
prompt=no
[dn]
CN=$NAME
[ext]
basicConstraints=critical,CA:false
keyUsage=critical,digitalSignature
extendedKeyUsage=critical,codeSigning
CFG

echo "▸ Création du certificat…"
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -config "$WORK/cfg" \
    -keyout "$WORK/key.pem" -out "$WORK/cert.pem" 2>/dev/null
# -legacy: the Keychain cannot read the PKCS#12 encryption OpenSSL 3 uses by default.
LEGACY=""
openssl pkcs12 -help 2>&1 | grep -q -- "-legacy" && LEGACY="-legacy"
openssl pkcs12 -export $LEGACY -name "$NAME" -passout pass:vgiom \
    -inkey "$WORK/key.pem" -in "$WORK/cert.pem" -out "$WORK/id.p12"

echo "▸ Ajout au trousseau de session…"
security import "$WORK/id.p12" -k "$KEYCHAIN" -P vgiom -T /usr/bin/codesign

echo "✓ Certificat « $NAME » prêt. Relancez le build."
