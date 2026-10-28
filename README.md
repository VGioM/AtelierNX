# Atelier NX

Interface graphique simple pour [nxnative](https://github.com/seedds/nxnative) (le projet d'origine), sans passer par NX2Apple.

- Liste les jeux Switch du dossier (`ROM/Switch`, plus `Updates/` et `DLC/`) d'après le `[title ID]` de leur nom.
- Construit une app Mac ou une IPA avec `scripts/build_ipa_from_nsp.py`, en choisissant la langue, la mise à jour et les DLC, avec la progression en direct (`--progress json`).
- Bouton « Jouer », journal du jeu, réglages forcés (`env.txt`), reprise des anciennes sauvegardes NX2Apple.
- Vérifie et installe les mises à jour de nxnative (`git pull`), et signale les jeux construits avec une ancienne version.

## Fonctionnement

- `server.py` : serveur local (127.0.0.1, jeton par session) qui lance nxnative ; bibliothèque standard Python seulement.
- `index.html` : la page.
- `App/main.swift` : la fenêtre Mac (WKWebView) qui démarre le serveur et le coupe en quittant.

Le script de nxnative attend un dossier par jeu : Atelier NX crée `nxnative-build/<title ID>/` avec des liens symboliques vers les fichiers choisis. Le dossier `<title ID>_build` que nxnative y crée garde l'extraction pour les builds suivants. Les apps vont dans `Apps/`.

Réglages et état : `~/Library/Application Support/AtelierNX/` (`config.json`, `state.json`, icônes).

## Compiler

```
./build.sh            # dist/Atelier NX.app
./build.sh install    # et copie dans /Applications
```

Sans l'app : `python3 server.py --open` ouvre la page dans le navigateur.
