#!/usr/bin/env python3
"""Atelier NX : serveur local qui pilote nxnative (seedds/nxnative) pour l'interface HTML.

Écoute sur 127.0.0.1 uniquement ; chaque requête /api doit porter le jeton injecté dans la page.
Lancé par Atelier NX.app (ou à la main : python3 server.py --open).
"""
import ast, json, os, plistlib, re, secrets, shlex, shutil, signal, struct, subprocess, sys, threading, time, argparse, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
SUPPORT = Path(os.environ.get('ATELIERNX_SUPPORT') or Path.home() / 'Library/Application Support/AtelierNX')   # autre dossier : tests
CONFIG = SUPPORT / 'config.json'
STATE = SUPPORT / 'state.json'
ICONS = SUPPORT / 'icons'
NX_SUPPORT = Path.home() / 'Library/Application Support/nxnative'

# Chemins vides = à choisir dans Réglages ; les dossiers d'apps et de build
# sont alors pris dans le dossier des jeux (voir config()).
DEFAULTS = {
    'nxnative': str(Path.home() / 'nxnative'),
    'python': '',
    'romDir': '',
    'keys': str(Path.home() / '.switch/prod.keys'),
    'outDir': '',
    'workDir': '',   # vide = <jeux>/nxnative-build.noindex (« .noindex » : Spotlight n'indexe pas les milliers de fichiers de build)
    'language': 'fr',
    'lowPriority': '1',   # builds avec nice : ils cèdent le processeur aux autres apps
    'gameDefaults': {},   # {DM_…: valeur, 'window.show': '0'/'1'} pré-remplis à chaque nouveau build Mac
    'cleanObsolete': '1',   # vider les dossiers « obsolete » de nxnative après chaque build réussi
    'cleanAfterBuild': '0',   # supprimer tous les fichiers de build d'un jeu après son build réussi (le suivant repart de zéro)
    'sharedShaders': '1',   # liste des shaders vus en jouant sur le disque des apps, commune à tous les Macs (shared_list)
}
GAME_EXT = {'.nsp', '.xci', '.nsz', '.xcz', '.nup'}   # .nup : conteneur NUP (jeu, mises à jour et DLC en un fichier, sans prod.keys)
TOKEN = secrets.token_urlsafe(24)
LOCK = threading.Lock()


def load_json(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def save_json(p, data):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp'); tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False)); tmp.replace(p)


def find_python(nxnative, venv=True):
    """Premier Python 3.12+ trouvé : venv du clone nxnative, puis Homebrew, python.org, /usr/local."""
    cands = [Path(nxnative) / '.venv/bin/python', Path(nxnative) / 'venv/bin/python'] if venv else []
    for d in ('/opt/homebrew/bin', '/Library/Frameworks/Python.framework/Versions/Current/bin', '/usr/local/bin'):
        cands += [Path(d) / f'python3.{m}' for m in range(20, 11, -1)]
    return next((str(c) for c in cands if c.exists()), 'python3')


def config():
    cfg = {**DEFAULTS, **load_json(CONFIG, {})}
    rom = cfg['romDir']
    for k, sub in (('outDir', 'Apps'), ('workDir', 'nxnative-build.noindex')):
        if not cfg[k] and rom:
            cfg[k] = str(Path(rom) / sub)
    if not cfg['python']:
        cfg['python'] = find_python(cfg['nxnative'])
    return cfg


def tool_env():
    env = dict(os.environ)
    # Une app lancée depuis le Finder n'a qu'un PATH minimal : xcodegen, zstd, glslang, dotnet sont dans Homebrew.
    env['PATH'] = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:' + env.get('PATH', '')
    env['PYTHONUNBUFFERED'] = '1'
    return env


def git(cfg, *args, timeout=120):
    r = subprocess.run(['git', '-C', cfg['nxnative'], *args], capture_output=True, text=True, timeout=timeout, env=tool_env())
    return r.returncode, (r.stdout + r.stderr).strip()


# ---------- modules Python de nxnative ----------
# nxnative ajoute parfois un module Python obligatoire (zbic en novembre 2026) : sans lui, chaque build échoue dès le départ.
# La liste est lue dans son script (check_prereqs : modules vérifiés et ligne « pip install … »), et les modules manquants
# sont installés dans le Python des réglages : au lancement, après une mise à jour de nxnative et avant chaque build.
# Filet de sécurité : un build qui échoue quand même sur un module manquant l'installe et relance le jeu une fois.
NX_SCRIPT = 'scripts/build_ipa_from_nsp.py'
FALLBACK_MODULES = ['lz4.block', 'capstone', 'fontTools', 'zbic', 'compression.zstd|backports.zstd']
FALLBACK_PIP = ['lz4', 'capstone>=5,<6', 'fonttools', 'zbic', 'backports.zstd']
PREREQ = {'key': None, 'missing': [], 'error': '', 'step': ''}   # dernier état connu, montré dans la page (step : installation en cours)
PREREQ_LOCK = threading.Lock()
MISSING_RE = re.compile(r'missing modules: ([\w., ]+?)\s*(?:\(pip install ([^;)]+)|$)')
TOOLS_RE = re.compile(r'tools not found|Metal compiler does not run|iOS SDK is not installed|Command Line Tools')   # check_prereqs


class PrereqError(ValueError):
    pass


def nx_requirements(cfg):
    """(modules, paquets pip) que demande nxnative ; « a|b » = l'un ou l'autre suffit."""
    try:
        src = (Path(cfg['nxnative']) / NX_SCRIPT).read_text()
    except OSError:
        return FALLBACK_MODULES, FALLBACK_PIP
    i = src.find('def check_prereqs')
    if i < 0:
        return FALLBACK_MODULES, FALLBACK_PIP
    j = src.find('\ndef ', i + 1)
    body = src[i:j if j > 0 else None]
    m = re.search(r'for m in \(([^)]*)\) if not _importable', body)
    mods = re.findall(r"'([\w.]+)'", m.group(1)) if m else FALLBACK_MODULES[:-1]
    if 'compression.zstd' in body and 'backports.zstd' in body:   # zstd : intégré à Python 3.14, sinon backports.zstd
        mods.append('compression.zstd|backports.zstd')
    m = re.search(r'pip install ([^;)\n]+)', body)
    try:
        pips = shlex.split(m.group(1)) if m else FALLBACK_PIP
    except ValueError:
        pips = FALLBACK_PIP
    return mods, pips


def missing_modules(cfg, mods):
    code = ('import importlib.util as u, json, sys\n'
            'def ok(m):\n try: return u.find_spec(m) is not None\n except Exception: return False\n'
            'print(json.dumps([g.split("|")[-1] for g in sys.argv[1:] if not any(ok(m) for m in g.split("|"))]))')
    r = subprocess.run([cfg['python'], '-I', '-c', code, *mods], capture_output=True, text=True, timeout=60, env=tool_env())
    if r.returncode:
        raise PrereqError(f"Python des réglages inutilisable ({cfg['python']}) : {(r.stderr or r.stdout).strip()[-300:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def pip_for(missing, pips):
    """Paquets pip à installer pour ces modules (lz4.block → lz4, fontTools → fonttools…)."""
    out = []
    for mod in missing:
        names = {mod.lower(), mod.split('.')[0].lower()}
        pkg = next((p for p in pips if re.split(r'[<>=!~\[ ]', p, maxsplit=1)[0].lower() in names), mod.split('.')[0])
        if pkg not in out:
            out.append(pkg)
    return out


def pip_install(cfg, pkgs):
    py, env = cfg['python'], tool_env()
    uv = shutil.which('uv', path=env['PATH'])
    if uv:   # les venv créés par uv n'ont pas pip
        cmd = [uv, 'pip', 'install', '--python', py, *pkgs]
    else:
        if subprocess.run([py, '-m', 'pip', '--version'], capture_output=True, env=env, timeout=60).returncode:
            subprocess.run([py, '-m', 'ensurepip', '--upgrade'], capture_output=True, env=env, timeout=300)
        cmd = [py, '-m', 'pip', 'install', *pkgs]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=900)
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-600:]


def ensure_prereqs(cfg, say=lambda msg, err=False: None, force=False, extra=None, platform=None, interactive=False):
    """Installe ce qui manque (modules Python, puis outils du Mac : voir ensure_system) ; renvoie la liste de ce qui a
    été installé. PrereqError si quelque chose manque encore. Les modules ne sont revérifiés que si le Python ou le script
    de nxnative changent ; les outils du Mac, rapides à vérifier, à chaque appel. extra : (modules, paquets) lus dans le
    message d'erreur d'un build. platform : 'ios' vérifie aussi xcodegen et le SDK iOS. interactive : peut ouvrir
    Terminal (Homebrew), l'App Store (Xcode) ou demander le mot de passe administrateur (xcode-select)."""
    with PREREQ_LOCK:
        installed = _ensure_modules(cfg, say, force, extra)
        try:
            installed += ensure_system(cfg, say, platform, interactive)
        except PrereqError as e:
            PREREQ.update(missing=['outils'], error=str(e))
            say(str(e), True)
            raise
        PREREQ.update(missing=[], error='')
        return installed


# ---------- outils du Mac ----------
# nxnative vérifie aussi Xcode, des outils Homebrew (xcodegen, zstd, glslang), la chaîne Metal (téléchargée à part depuis
# Xcode 26) et, pour l'iPhone, le SDK iOS : sans eux, chaque build échoue en une seconde. La liste des outils est lue dans
# son check_prereqs (dict « tools = {outil: 'brew install paquet' | 'Xcode'} »), comme les modules Python.
FALLBACK_TOOLS = {'xcodebuild': 'Xcode', 'xcodegen': 'brew install xcodegen', 'zstd': 'brew install zstd',
                  'make': 'Xcode', 'glslangValidator': 'brew install glslang'}
BREW_URL = 'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh'
XCODE_STORE = 'macappstore://apps.apple.com/app/id497799835'
OPENED = set()   # Terminal (Homebrew) ou App Store (Xcode) déjà ouverts pendant cette session : pas à chaque build


def nx_tools(cfg):
    try:
        src = (Path(cfg['nxnative']) / NX_SCRIPT).read_text()
        i = src.find('def check_prereqs'); j = src.find('\ndef ', i + 1)
        m = re.search(r'tools = (\{[^}]*\})', src[i:j if j > 0 else None]) if i >= 0 else None
        tools = ast.literal_eval(m.group(1)) if m else None
        if isinstance(tools, dict) and tools:
            return tools
    except (OSError, ValueError, SyntaxError):
        pass
    return dict(FALLBACK_TOOLS)


def brew_bin():
    return shutil.which('brew', path=tool_env()['PATH'])


def sh_run(cmd, timeout=600, **kw):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=tool_env(), timeout=timeout, **kw)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


def as_admin(shell_cmd):
    """Commande lancée avec le mot de passe administrateur (fenêtre de macOS). Renvoie (code, sortie)."""
    script = f'do shell script {json.dumps(shell_cmd)} with administrator privileges'
    return sh_run(['osascript', '-e', script], timeout=1800)


def open_once(what, cmd):
    if what not in OPENED:
        OPENED.add(what)
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def brew_install(pkgs, say):
    brew = brew_bin()
    say(f"Installation avec Homebrew : {' '.join(pkgs)}…")
    code, outp = sh_run([brew, 'install', *pkgs], timeout=3600, input='')
    return code == 0, outp[-600:]


def need_homebrew(interactive, what):
    if interactive:
        open_once('brew', ['osascript', '-e', 'tell application "Terminal" to activate', '-e',
                           f'tell application "Terminal" to do script {json.dumps(f"/bin/bash -c \"$(curl -fsSL {BREW_URL})\"")}'])
        how = ("Son installation vient de s'ouvrir dans Terminal (elle demande ton mot de passe) ; "
               "quand elle est finie, clique sur « Réessayer l'installation ».")
    else:
        how = 'Lance un build ou « Réessayer l\'installation » pour ouvrir son installation dans Terminal.'
    raise PrereqError(f'Homebrew est nécessaire pour installer {what}, et il n\'est pas installé. {how}')


def ensure_xcode(say, interactive):
    """Xcode installé, sélectionné (pas seulement les Command Line Tools), licence acceptée et premier lancement fait."""
    dev = sh_run(['xcode-select', '-p'], timeout=30)[1]
    code, outp = sh_run(['xcodebuild', '-version'], timeout=60)
    if code == 0 and 'CommandLineTools' not in dev:
        return []
    apps = sorted(Path('/Applications').glob('Xcode*.app'), key=lambda p: (p.name != 'Xcode.app', p.name))
    if not apps:
        if interactive:
            open_once('xcode', ['open', XCODE_STORE])
        raise PrereqError("Xcode n'est pas installé : nxnative s'en sert pour compiler. "
                          + ("Sa page vient de s'ouvrir dans l'App Store ; " if interactive else "Installe-le depuis l'App Store ; ")
                          + "une fois installé, ouvre-le une fois puis clique sur « Réessayer l'installation ».")
    if not interactive:
        raise PrereqError(f"Xcode est installé ({apps[0]}) mais pas prêt (licence ou sélection) : "
                          "clique sur « Réessayer l'installation » (mot de passe administrateur demandé).")
    say("Préparation d'Xcode (sélection, licence, premier lancement) : macOS demande le mot de passe administrateur…")
    x = shlex.quote(str(apps[0]))
    code, outp = as_admin(f'xcode-select -s {x} && xcodebuild -license accept && xcodebuild -runFirstLaunch')
    if sh_run(['xcodebuild', '-version'], timeout=60)[0]:
        raise PrereqError(f"Xcode n'a pas pu être préparé ({outp[-300:] or 'annulé'}). "
                          f"À la main : sudo xcode-select -s {x} && sudo xcodebuild -license accept")
    return ['Xcode (licence et sélection)']


def ensure_component(say, check, download, label, size):
    if sh_run(check, timeout=60)[0] == 0:
        return []
    say(f"Il manque : {label}. Téléchargement par Xcode ({size}, quelques minutes)…")
    outp = download_with_progress(download, label, say)
    if sh_run(check, timeout=60)[0]:   # xcodebuild -downloadComponent sort parfois avec 0 même en échec
        tail = '\n'.join(l for l in outp.splitlines() if 'error' in l.lower())[-400:] or outp[-400:]
        raise PrereqError(f"{label} : téléchargement impossible ({shlex.join(download)}) :\n{tail}\n"
                          "Vérifie la connexion (ou passe par Xcode → Réglages → Composants), puis « Réessayer l'installation ».")
    say(f'Installé : {label}.')
    return [label]


def download_with_progress(cmd, label, say):
    """xcodebuild -downloadComponent/-downloadPlatform écrit « Downloading... (12 MB of 838,9 MB downloaded) » ;
    la progression est relayée toutes les 2 s. Renvoie la fin de la sortie."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=tool_env())
    except OSError as e:
        return str(e)
    buf, last = '', 0.0
    while chunk := p.stdout.read(512):
        buf = (buf + chunk.decode(errors='replace'))[-4000:]
        m = re.findall(r'\(([\d.,]+ \w+) of ([\d.,]+ \w+) downloaded\)', buf)
        if m and time.time() - last > 2:
            last = time.time(); say(f'{label} : {m[-1][0]} sur {m[-1][1]}…')
    p.wait()
    return re.sub(r'Downloading\.\.\. \([^)]*\)', '', buf).strip()


def ensure_system(cfg, say=lambda msg, err=False: None, platform=None, interactive=False):
    """Ce que nxnative vérifie avant chaque build (check_prereqs), installé quand c'est possible."""
    ios = platform == 'ios'
    installed = ensure_xcode(say, interactive)
    tools = nx_tools(cfg)
    if not ios:
        tools.pop('xcodegen', None)   # nxnative ne s'en sert que pour le projet Xcode de l'IPA
    path = tool_env()['PATH']
    gone = {t: how for t, how in tools.items() if not shutil.which(t, path=path)}
    brew_pkgs = [how.split()[-1] for how in gone.values() if how.startswith('brew install')]
    other = [f'{t} ({how})' for t, how in gone.items() if not how.startswith('brew install')]
    if other:
        raise PrereqError('Outils introuvables pour nxnative : ' + ', '.join(other) + '.')
    if brew_pkgs:
        if not brew_bin():
            need_homebrew(interactive, ' '.join(brew_pkgs))
        ok, outp = brew_install(brew_pkgs, say)
        still = [t for t in gone if not shutil.which(t, path=path)]
        if still:
            raise PrereqError(f"Installation impossible avec Homebrew (brew install {' '.join(brew_pkgs)}) :\n{outp}")
        say(f"Installé avec Homebrew : {' '.join(brew_pkgs)}")
        installed += brew_pkgs
    installed += ensure_component(say, ['xcrun', '-sdk', 'macosx', 'metal', '--version'],
                                  ['xcodebuild', '-downloadComponent', 'MetalToolchain'], 'Chaîne Metal', 'environ 850 Mo')
    if ios:
        installed += ensure_component(say, ['xcrun', '--sdk', 'iphoneos', '--show-sdk-version'],
                                      ['xcodebuild', '-downloadPlatform', 'iOS'], 'SDK iOS', 'plusieurs Go')
    return installed


def _ensure_modules(cfg, say, force, extra):
    """Modules Python de nxnative (résultat gardé tant que le Python et le script ne changent pas)."""
    try:
        mtime = (Path(cfg['nxnative']) / NX_SCRIPT).stat().st_mtime
    except OSError:
        mtime = None
    key = (cfg['python'], cfg['nxnative'], mtime)
    if not force and PREREQ['key'] == key and not PREREQ['missing']:
        return []
    mods, pips = nx_requirements(cfg)
    if extra:
        mods = mods + [m for m in extra[0] if m not in mods]; pips = extra[1] + pips
    missing = []
    try:
        missing = missing_modules(cfg, mods)
        installed = []
        if missing:
            pkgs = pip_for(missing, pips)
            say(f"nxnative a besoin de modules Python absents : {', '.join(missing)}. Installation de {' '.join(pkgs)}…")
            ok, outp = pip_install(cfg, pkgs)
            still = missing_modules(cfg, mods)
            if still:
                raise PrereqError(f"Modules Python manquants pour nxnative : {', '.join(still)}. Installation impossible"
                                  f" ({shlex.join([cfg['python'], '-m', 'pip', 'install', *pip_for(still, pips)])}) :\n{outp}")
            installed = missing
            say(f"Modules installés : {', '.join(missing)}")
        PREREQ.update(key=key, missing=[], error='')
        return installed
    except PrereqError as e:
        PREREQ.update(key=key, missing=missing or ['?'], error=str(e))
        say(str(e), True)
        raise


# ---------- installation de nxnative ----------
# Sur un Mac où nxnative n'est pas encore là (autre utilisateur, nouveau Mac), la page propose de l'installer dans le
# dossier des Réglages (~/nxnative par défaut) : clone git, Python 3.12+ (Homebrew au besoin), environnement .venv, modules
# Python, puis les outils du Mac (Xcode, outils Homebrew, chaîne Metal : ensure_system).
NX_REPO = 'https://github.com/seedds/nxnative'
NX_INSTALL = {'running': False, 'step': '', 'error': '', 'note': ''}


def nx_present(cfg):
    return (Path(cfg['nxnative']) / NX_SCRIPT).exists()


def install_nxnative():
    cfg = config()
    target = Path(cfg['nxnative']).expanduser()
    step = lambda s: NX_INSTALL.update(step=s)
    try:
        if not nx_present(cfg):
            if target.exists() and (not target.is_dir() or any(target.iterdir())):
                raise ValueError(f"Le dossier {target} existe déjà et ne contient pas nxnative. "
                                 "Choisis un autre dossier nxnative dans Réglages (ou vide celui-ci).")
            if not shutil.which('git', path=tool_env()['PATH']):
                raise ValueError('git est introuvable : installe Xcode (ou xcode-select --install), puis réessaie.')
            part = target.with_name(target.name + '.partial')
            shutil.rmtree(part, ignore_errors=True)
            part.parent.mkdir(parents=True, exist_ok=True)
            # Sans les anciennes versions des fichiers (--filter=blob:none) : l'historique est complet pour les messages
            # de commit et git pull, mais le téléchargement est bien plus léger.
            p = subprocess.Popen(['git', 'clone', '--progress', '--filter=blob:none', NX_REPO, str(part)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=tool_env())
            tail, buf = [], b''
            while chunk := p.stderr.read(256):
                buf += chunk
                *lines, buf = re.split(rb'[\r\n]', buf)
                for l in (x.decode(errors='replace').strip() for x in lines):
                    if not l:
                        continue
                    tail = (tail + [l])[-5:]
                    m = re.match(r'Receiving objects:\s+(\d+)%', l)
                    if m:
                        step(f'Téléchargement de nxnative : {m.group(1)} %')
                    elif l.startswith(('Resolving deltas', 'Updating files')):
                        step('Écriture des fichiers de nxnative…')
            if p.wait():
                shutil.rmtree(part, ignore_errors=True)
                raise ValueError('Échec du téléchargement de nxnative :\n' + '\n'.join(tail))
            if target.is_dir():
                target.rmdir()   # dossier vide
            part.rename(target)
        if not load_json(CONFIG, {}).get('python') and not (target / '.venv/bin/python').exists():
            base = find_python(target, venv=False)
            if base == 'python3':   # pas de Python 3.12+ : celui de Homebrew
                if not brew_bin():
                    try:
                        need_homebrew(True, 'Python 3.13')
                    except PrereqError as e:
                        raise ValueError(str(e))
                step('Installation de Python 3.13 avec Homebrew…')
                ok, outp = brew_install(['python@3.13'], lambda m, err=False: None)
                base = find_python(target, venv=False)
                if base == 'python3':
                    raise ValueError(f'Installation de Python 3.13 impossible (brew install python@3.13) :\n{outp}')
            step('Création de l\'environnement Python…')
            r = subprocess.run([base, '-m', 'venv', str(target / '.venv')], capture_output=True, text=True, env=tool_env(), timeout=300)
            if r.returncode:
                raise ValueError(f'Création de l\'environnement Python impossible : {(r.stderr or r.stdout).strip()[-300:]}')
        cfg = config()
        step('Installation des modules Python…')
        ok, outp = pip_install(cfg, ['nsz', 'pycryptodome'])   # .nsz/.xcz et en-têtes NCA, en plus de ceux du script
        if not ok:
            raise ValueError(f'Installation de nsz et pycryptodome impossible :\n{outp}')
        try:   # modules Python, puis Xcode, outils Homebrew et chaîne Metal
            ensure_prereqs(cfg, say=lambda m, err=False: err or step(m), force=True, interactive=True)
        except PrereqError as e:
            raise ValueError(f'nxnative est installé, mais pas encore tout ce qu\'il faut pour construire :\n{e}')
        NX_INSTALL['note'] = 'nxnative est installé, avec tout ce qu\'il faut pour construire.'
        build_catalog(cfg, fetch=False)
    except Exception as e:
        NX_INSTALL['error'] = str(e)
    finally:
        NX_INSTALL.update(running=False, step='')


def start_nx_install():
    with LOCK:
        if NX_INSTALL['running']:
            return
        NX_INSTALL.update(running=True, step='Préparation…', error='', note='')
    threading.Thread(target=install_nxnative, daemon=True).start()


# ---------- bibliothèque ----------

SKIP_DIRS = {'nxnative-build', 'nxnative-build.noindex', 'Apps', 'save', 'log', 'Mods', 'imgs'}
ARCHIVE_EXT = {'.zip', '.rar', '.7z'}
IDS_CACHE = SUPPORT / 'ids.json'
NOISE = r'\b(EUR|Eur|USA|US|JPN|JP|NSP|XCI|XCi|xci|nsp|CLC|Multi|UPD|Update|DLC|Switch|Base)\b'


def clean_name(fname):
    n = Path(fname).stem
    n = re.sub(r'\[[^\]]*\]|\([^)]*\)', ' ', n)
    n = re.sub(r'\b[vV]\d+(\.\d+)+\b|' + NOISE, ' ', n)
    n = re.sub(r'\s+', ' ', n).strip(' -_+')
    n = re.sub(r'(\s-)+$', '', n).strip()
    m = re.match(r'^(.*), (The|Les?|La)( - .*)?$', n)   # « Legend of Zelda, The - X » → « The Legend of Zelda - X »
    if m:
        n = f'{m.group(2)} {m.group(1)}{m.group(3) or ""}'
    return n


CHANGES = {}   # (jeu, commit du build, HEAD) -> résumé : l'historique ne change pas, seul HEAD avance


def first_sentence(s, cap=260):
    """Début d'un message de commit nxnative (souvent un long paragraphe sur une ligne)."""
    m = re.search(r'(?<=[a-z0-9)`\'"])[.;:](\s)', s)
    s = s[:m.start() + 1] if m and m.start() > 30 else s
    return s if len(s) <= cap else s[:cap].rsplit(' ', 1)[0] + '…'


def game_changes(cfg, gid, name, commit, head):
    """Ce qui a changé dans nxnative depuis le build d'un jeu : le nombre de commits, et ceux qui le
    concernent (son profil dm/games/<id>, ou son nom ou son identifiant dans le message)."""
    key = (gid, commit, head)
    if key in CHANGES:
        return CHANGES[key]
    code, n = git(cfg, 'rev-list', '--count', f'{commit}..{head}')
    if code or not n.isdigit():
        return None   # commit du build absent du clone (historique réécrit, autre clone)
    names = {gid, name or ''}
    for sep in (': ', ' - ', ' – '):   # « The Legend of Zelda: Link's Awakening » -> « Link's Awakening »
        if name and sep in name:
            names.add(name.split(sep, 1)[1])
    names = sorted(x for x in names if len(x) >= 5)
    fmt = ['--format=%H%x1f%cd%x1f%s', '--date=format:%d/%m']
    seen, items = set(), []
    outs = [git(cfg, 'log', *fmt, f'{commit}..{head}', '--', f'dm/games/{gid}')[1]]
    if names:
        outs.append(git(cfg, 'log', *fmt, '-i', '-F', *[f'--grep={x}' for x in names], f'{commit}..{head}')[1])
    for out in outs:
        for line in out.splitlines():
            parts = line.split('\x1f')
            if len(parts) == 3 and parts[0] not in seen:
                seen.add(parts[0])
                items.append({'h': parts[0][:8], 'date': parts[1], 's': first_sentence(parts[2]), 'full': parts[2][:4000]})
    order = git(cfg, 'rev-list', f'{commit}..{head}')[1].splitlines()
    rank = {h[:8]: i for i, h in enumerate(order)}
    items.sort(key=lambda x: rank.get(x['h'], 0))   # du plus récent au plus ancien
    CHANGES[key] = res = {'total': int(n), 'game': len(items), 'items': items[:25]}
    return res


def profile_name(cfg, gid):
    """Nom officiel du jeu, lu dans le titre du README de son profil nxnative."""
    try:
        first = (Path(cfg['nxnative']) / 'dm/games' / gid / 'README.md').read_text().splitlines()[0]
        m = re.match(r'#\s+(.+?)(?:\s+\(`.*)?$', first)
        return m.group(1).strip() if m else None
    except (OSError, IndexError):
        return None


def prod_keys(cfg):
    """Toutes les clés de prod.keys ({nom: octets}) ; {} si le fichier manque."""
    keys = {}
    try:
        for line in Path(cfg['keys']).read_text().splitlines():
            k, _, v = line.replace(' ', '').partition('=')
            try:
                keys[k.lower()] = bytes.fromhex(v)
            except ValueError:
                pass
    except OSError:
        pass
    return keys


def header_key(cfg):
    return prod_keys(cfg).get('header_key')


def xts_decrypt(key, data, sector, ssize=0x200):
    """AES-128-XTS des en-têtes NCA (numéro de secteur en gros-boutiste, comme Nintendo)."""
    from Crypto.Cipher import AES
    k1, k2 = AES.new(key[:16], AES.MODE_ECB), AES.new(key[16:32], AES.MODE_ECB)
    out = bytearray()
    for s in range(len(data) // ssize):
        t = int.from_bytes(k2.encrypt((sector + s).to_bytes(16, 'big')), 'little')
        blk = data[s * ssize:(s + 1) * ssize]
        for j in range(0, ssize, 16):
            tb = t.to_bytes(16, 'little')
            c = bytes(x ^ y for x, y in zip(blk[j:j + 16], tb))
            out += bytes(x ^ y for x, y in zip(k1.decrypt(c), tb))
            t <<= 1
            if t >> 128:
                t = (t & ((1 << 128) - 1)) ^ 0x87
    return bytes(out)


def pfs0_entries(f):
    f.seek(0); h = f.read(16)
    n, st = struct.unpack_from('<II', h, 4)
    ent, strs = f.read(n * 0x18), f.read(st)
    data = 16 + n * 0x18 + st
    out = {}
    for i in range(n):
        o, size, no = struct.unpack_from('<QQI', ent, i * 0x18)
        out[strs[no:strs.index(b'\0', no)].decode(errors='replace')] = (data + o, size)
    return out


def nca_title_ids(f, entries, hk):
    ids = set()
    for name, (o, _) in entries.items():
        if name.endswith('.nca'):
            f.seek(o)
            dec = xts_decrypt(hk, f.read(0x400), 0)
            if dec[0x200:0x204] in (b'NCA3', b'NCA2'):
                ids.add(struct.unpack_from('<Q', dec, 0x210)[0])
    return ids


def pick_id(ids):
    bases = sorted(i for i in ids if i & 0xFFF == 0)
    return bases[0] if bases else (min(ids) if ids else None)


def hfs0(f, off):
    f.seek(off); h = f.read(16)
    if h[:4] != b'HFS0':
        raise ValueError('HFS0 attendu')
    n, st = struct.unpack_from('<II', h, 4)
    ents, strs = f.read(0x40 * n), f.read(st)
    data = off + 16 + 0x40 * n + st
    out = {}
    for i in range(n):
        o, size, no = struct.unpack_from('<QQI', ents, i * 0x40)
        out[strs[no:strs.index(b'\0', no)].decode(errors='replace')] = (data + o, size)
    return out


def xci_title_ids(f, hk):
    f.seek(0x130); root = struct.unpack('<Q', f.read(8))[0]
    sec = hfs0(f, root)['secure'][0]
    return nca_title_ids(f, hfs0(f, sec), hk)


def read_title_id(path, hk):
    """Identifiant lu dans le fichier : ticket d'un NSP/NSZ, sinon en-têtes NCA (NSP sans ticket, XCI/XCZ) ; sinon None."""
    with open(path, 'rb') as f:
        h = f.read(0x104)
        if h[:4] == b'PFS0':
            entries = pfs0_entries(f)
            for n in entries:
                if re.match(r'[0-9A-Fa-f]{32}\.tik$', n):
                    return int(n[:16], 16)
            return pick_id(nca_title_ids(f, entries, hk)) if hk else None
        if h[0x100:0x104] == b'HEAD' and hk:
            return pick_id(xci_title_ids(f, hk))
    return None


# ---------- icône lue dans le fichier du jeu (NCA Control, RomFS, icon_<Langue>.dat) ----------
# Format d'après nsz (Fs/Nca.py, BaseFs.py, Ivfc.py, Ticket.py, nut/Keys.py) et nx_extract de nxnative (fonction Icon).
ICON_LANG = {'fr': 'French', 'fr-CA': 'CanadianFrench', 'en-US': 'AmericanEnglish', 'en-GB': 'BritishEnglish',
             'es': 'Spanish', 'es-419': 'LatinAmericanSpanish', 'de': 'German', 'it': 'Italian', 'nl': 'Dutch',
             'pt': 'Portuguese', 'pt-BR': 'BrazilianPortuguese', 'ru': 'Russian', 'ja': 'Japanese', 'ko': 'Korean',
             'zh-Hans': 'SimplifiedChinese', 'zh-Hant': 'TraditionalChinese'}
TICKET_SIG = {0x10000: 0x200, 0x10003: 0x200, 0x10001: 0x100, 0x10004: 0x100, 0x10002: 0x3C, 0x10005: 0x3C}


def _ecb(key):
    from Crypto.Cipher import AES
    return AES.new(key, AES.MODE_ECB)


def titlekek(keys, mk):
    k = keys.get(f'titlekek_{mk:02x}')
    if k is None and f'master_key_{mk:02x}' in keys and 'titlekek_source' in keys:
        k = _ecb(keys[f'master_key_{mk:02x}']).decrypt(keys['titlekek_source'])
    return k


def key_area_key(keys, kind, mk):
    k = keys.get(f'key_area_key_{kind}_{mk:02x}')
    src = keys.get(f'key_area_key_{kind}_source')
    if k is None and src and all(n in keys for n in (f'master_key_{mk:02x}', 'aes_kek_generation_source', 'aes_key_generation_source')):
        kek = _ecb(keys[f'master_key_{mk:02x}']).decrypt(keys['aes_kek_generation_source'])
        k = _ecb(_ecb(kek).decrypt(src)).decrypt(keys['aes_key_generation_source'])
    return k


def control_nca(f, entries, hk, tid):
    """(offset, en-tête déchiffré 0x600) du NCA Control du titre, ou None."""
    for name, (o, size) in entries.items():
        if not name.lower().endswith('.nca') or size < 0xC00:
            continue
        f.seek(o)
        raw = f.read(0x600)
        h = xts_decrypt(hk, raw[:0x400], 0)
        if h[0x200:0x204] == b'NCA3' and h[0x205] == 2 and struct.unpack_from('<Q', h, 0x210)[0] == tid:
            return o, h + xts_decrypt(hk, raw[0x400:], 2)
    return None


def ticket_titlekey(f, entries, rights_id):
    e = next((v for n, v in entries.items() if n.lower() == rights_id.hex() + '.tik'), None)
    if not e:
        return None
    f.seek(e[0]); t = f.read(min(e[1], 0x400))
    sig = TICKET_SIG.get(struct.unpack_from('<I', t, 0)[0])
    if not sig:
        return None
    pos = 4 + sig + (0x40 - (sig + 4) % 0x40) + 0x40
    return t[pos:pos + 16] if len(t) >= pos + 16 else None


def romfs_icon(path, keys, tid, lang):
    """Octets JPEG de l'icône (langue des Réglages, sinon anglais, sinon la première), ou None."""
    from Crypto.Cipher import AES
    hk = keys['header_key']
    with open(path, 'rb') as f:
        head = f.read(0x104)
        if head[:4] == b'PFS0':
            entries = pfs0_entries(f)
        elif head[0x100:0x104] == b'HEAD':
            f.seek(0x130); root = struct.unpack('<Q', f.read(8))[0]
            entries = hfs0(f, hfs0(f, root)['secure'][0])
        else:
            return None
        found = control_nca(f, entries, hk, tid)
        if not found:
            return None
        base, h = found
        mk = max(max(h[0x206], h[0x220]) - 1, 0)
        rights = h[0x230:0x240]
        if any(rights):
            enc, kek = ticket_titlekey(f, entries, rights), titlekek(keys, mk)
        else:
            enc = h[0x320:0x330]
            kek = key_area_key(keys, ('application', 'ocean', 'system')[h[0x207]] if h[0x207] < 3 else '', mk)
        if not enc or not kek:
            return None
        key = _ecb(kek).decrypt(enc)
        start = struct.unpack_from('<I', h, 0x240)[0] * 0x200
        sh = h[0x400:0x600]
        if sh[2] != 0 or sh[4] != 3 or sh[0x8:0xC] != b'IVFC':   # version u16, fs_type (0 = RomFS), hash, chiffrement (3 = CTR)
            return None
        ctr_hi = sh[0x140:0x148][::-1]

        def read(off, size):   # off relatif à la section ; AES-CTR aligné sur 16 octets
            a = start + off
            al = a & ~0xF
            f.seek(base + al)
            data = f.read(size + (a - al) + 15 & ~0xF)
            c = AES.new(key, AES.MODE_CTR, nonce=b'', initial_value=ctr_hi + (al >> 4).to_bytes(8, 'big'))
            return c.decrypt(data)[a - al:a - al + size]

        n = struct.unpack_from('<I', sh, 0x8 + 0xC)[0]
        romfs = struct.unpack_from('<Q', sh, 0x8 + 0x10 + (n - 2) * 0x18)[0]
        rh = struct.unpack('<10Q', read(romfs, 0x50))
        meta = read(romfs + rh[7], rh[8])
        files, i = {}, 0
        while i + 0x20 <= len(meta):
            off, size = struct.unpack_from('<QQ', meta, i + 8)
            ln = struct.unpack_from('<I', meta, i + 0x1C)[0]
            files[meta[i + 0x20:i + 0x20 + ln].decode(errors='replace')] = (off, size)
            i += 0x20 + (ln + 3 & ~3)
        icons = [n for n in files if n.startswith('icon_') and n.endswith('.dat')]
        want = [f'icon_{l}.dat' for l in (ICON_LANG.get(lang), 'AmericanEnglish', 'BritishEnglish') if l]
        name = next((w for w in want if w in files), icons[0] if icons else None)
        if not name or not 0 < files[name][1] <= 2_000_000:
            return None
        data = read(romfs + rh[9] + files[name][0], files[name][1])
        return data if data[:2] == b'\xff\xd8' else None


_icon_lock = threading.Lock()
_icon_tried = set()


def fetch_icons(cfg, games):
    """Icônes manquantes : lues dans le NCA Control du fichier de base (NSP ou XCI), en arrière-plan, une fois par jeu."""
    try:
        import Crypto.Cipher.AES   # noqa: F401
    except Exception:
        return
    keys = prod_keys(cfg)
    if 'header_key' not in keys:
        return
    todo = [(gid, path) for gid, path in games if gid not in _icon_tried
            and Path(path).suffix.lower() in ('.nsp', '.xci')]   # NSZ/XCZ (Control en .ncz), NUP : pas d'icône
    if not todo:
        return
    _icon_tried.update(gid for gid, _ in todo)
    lang = cfg.get('language') or 'fr'

    def run():
        with _icon_lock:
            for gid, path in todo:
                if icon_url(gid):
                    continue
                try:
                    data = romfs_icon(path, keys, int(gid, 16), lang)
                    if data:
                        ICONS.mkdir(parents=True, exist_ok=True)
                        tmp = ICONS / f'.{gid}.jpg.tmp'
                        tmp.write_bytes(data)
                        tmp.replace(ICONS / f'{gid}.jpg')
                except Exception:
                    pass
    threading.Thread(target=run, daemon=True).start()


def version_from_name(name):
    for pat in (r'\[v(\d+)\]', r'\(v(\d+)\)', r'\[(\d{5,})\]'):
        m = re.search(pat, name)
        if m and int(m.group(1)) % 65536 == 0:
            return int(m.group(1))
    return None


# Un .nup se lit avec le module de nxnative (scripts/pack_nup.py), dans le Python des réglages : identifiant du programme
# construit (celui de la Switch 2 pour une Switch 2 Edition), versions de mise à jour et DLC qu'il contient.
NUP_PROBE = ('import sys, json\nsys.path.insert(0, sys.argv[1])\nimport pack_nup as p\nn = p.Nup(sys.argv[2])\n'
             'try:\n    app, base, vers, dlc = p.scan(n)\n'
             '    print(json.dumps({"tid": "%016X" % app, "versions": sorted(vers), "dlc": len(dlc)}))\nfinally:\n    n.close()\n')


def nup_info(cfg, path):
    r = subprocess.run([cfg['python'], '-I', '-c', NUP_PROBE, str(Path(cfg['nxnative']) / 'scripts'), str(path)],
                       capture_output=True, text=True, timeout=120, env=tool_env())
    if r.returncode:
        raise ValueError((r.stderr.strip().splitlines() or ['NUP illisible'])[-1])
    return json.loads(r.stdout)


def identify(cfg, path, cache, hk):
    st = path.stat()
    key = f'{path}|{st.st_size}|{int(st.st_mtime)}'
    if key in cache:
        return cache[key]
    if path.suffix.lower() == '.nup':
        try:
            info = nup_info(cfg, path)
            cache[key] = r = {'tid': info['tid'], 'ver': 0, 'nup': {'versions': info['versions'], 'dlc': info['dlc']}}
            return r
        except Exception:
            m = re.search(r'\[([0-9A-Fa-f]{16})\]', path.name)   # nxnative absent ou trop ancien : le nom, sans le garder
            return {'tid': m.group(1).upper() if m else None, 'ver': 0, 'nup': {'versions': [], 'dlc': 0}}
    tid = None
    m = re.search(r'\[([0-9A-Fa-f]{16})\]', path.name)
    try:
        tid = read_title_id(path, hk)   # le ticket d'abord : l'étiquette d'un nom de fichier peut être fausse
    except Exception:
        pass
    if tid is None and m:
        tid = int(m.group(1), 16)
    r = {'tid': f'{tid:016X}' if tid is not None else None, 'ver': version_from_name(path.name)}
    if tid is not None:   # un échec n'est pas gardé : il sera retenté (prod.keys ajouté entre-temps, par exemple)
        cache[key] = r
    return r


def walk(rom):
    for root, dirs, files in os.walk(rom):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in SKIP_DIRS
                         and not d.endswith('_build') and not d.endswith('.app'))
        for f in sorted(files):
            if not f.startswith('.'):
                yield Path(root) / f


def same_commit(a, b):
    """Deux identifiants de commit abrégés à des longueurs différentes (git rev-parse --short, DMCommit de l'app)."""
    a, b = (a or '').lower(), (b or '').lower()
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def adopt_builds(cfg, state):
    """Apps Mac du dossier des apps absentes de state.json (construites sur un autre Mac, ou avant une
    réinstallation) : le jeu et le commit nxnative sont lus dans leur Info.plist (CFBundleIdentifier
    org.nxnative.<id>, DMCommit). Renvoie True si state a été complété."""
    out_dir = Path(cfg['outDir']) if cfg.get('outDir') else None
    if not out_dir or not out_dir.is_dir():
        return False
    known = {Path(b['out']).name for v in state.values() for b in v.get('builds', {}).values() if b.get('out')}
    added = False
    for app in sorted(out_dir.glob('*.app')):
        if app.name in known:
            continue
        try:
            with open(app / 'Contents/Info.plist', 'rb') as f:
                info = plistlib.load(f)
        except Exception:
            continue
        m = re.fullmatch(r'org\.nxnative\.([0-9a-fA-F]{16})', str(info.get('CFBundleIdentifier', '')))
        commit = str(info.get('DMCommit', ''))
        if not m or not re.fullmatch(r'[0-9a-fA-F]{7,40}', commit):
            continue
        gid = m.group(1).upper()
        builds = state.setdefault(gid, {}).setdefault('builds', {})
        if 'mac-app' in builds and Path(builds['mac-app']['out']).exists():
            continue   # le jeu a déjà une app connue (deux copies dans le dossier)
        builds['mac-app'] = {'out': str(app), 'language': str(info.get('DMLanguage', '')), 'commit': commit[:12],
                             'built': time.strftime('%Y-%m-%d %H:%M', time.localtime(app.stat().st_mtime)),
                             'found': True}
        added = True
    return added


def scan(cfg):
    if not cfg['romDir']:
        return [], [], []
    rom = Path(cfg['romDir'])
    cache = load_json(IDS_CACHE, {})
    n0 = len(cache)
    hk = header_key(cfg)
    # Sans header_key, un XCI ou un NSP sans ticket ne se lit que par l'identifiant [0100…] de son nom.
    no_keys = (None if hk else f"prod.keys introuvable ({cfg['keys']}) : il faut ses clés pour lire ce fichier, "
               "dont le nom ne contient pas l'identifiant [0100…]" if not Path(cfg['keys']).is_file()
               else f"header_key absente de prod.keys ({cfg['keys']}) : il faut cette clé pour lire ce fichier")
    games, orphans, archives = {}, [], []
    for f in walk(rom):
        ext = f.suffix.lower()
        if ext in ARCHIVE_EXT:
            archives.append({'path': str(f), 'name': f.name, 'rel': str(f.relative_to(rom)), 'size': f.stat().st_size})
            continue
        if ext not in GAME_EXT or not f.is_file():
            continue
        size = f.stat().st_size
        rel = str(f.relative_to(rom))
        if size == 0:
            orphans.append({'path': str(f), 'name': f.name, 'rel': rel, 'why': 'fichier vide'})
            continue
        r = identify(cfg, f, cache, hk)
        if not r['tid']:
            why = no_keys if no_keys and ext != '.nup' else 'identifiant illisible'
            orphans.append({'path': str(f), 'name': f.name, 'rel': rel, 'why': why, 'keys': why == no_keys})
            continue
        tid = int(r['tid'], 16)
        low = tid & 0xFFF
        if ext == '.nup':   # toujours un jeu complet ; un programme Switch 2 a un index non nul dans les bits bas
            kind, base = 'base', tid
        elif low == 0:
            kind, base = 'base', tid
        elif low == 0x800:
            kind, base = 'update', tid & ~0xFFF
        else:
            kind, base = 'dlc', (tid & ~0xFFF) - 0x1000
        e = {'path': str(f), 'name': f.name, 'rel': rel, 'kind': kind, 'tid': r['tid'],
             'ver': 0 if kind == 'base' else r['ver'], 'size': size, 'ext': ext}
        if ext == '.nup':
            e['nup'] = r.get('nup') or {'versions': [], 'dlc': 0}
        g = games.setdefault(f'{base:016X}', {'base': None, 'bases': [], 'updates': [], 'dlc': []})
        if kind == 'base':
            g['bases'].append(e)
        elif kind == 'update':
            g['updates'].append(e)
        else:
            g['dlc'].append(e)
    if len(cache) != n0:
        save_json(IDS_CACHE, cache)
    with LOCK:   # un build qui se termine écrit aussi state.json
        state = load_json(STATE, {})
        if adopt_builds(cfg, state):
            save_json(STATE, state)
    state_moved = False
    profiles = Path(cfg['nxnative']) / 'dm/games'
    head = git(cfg, 'rev-parse', '--short', 'HEAD')[1] if (Path(cfg['nxnative']) / '.git').exists() else ''
    head_full = git(cfg, 'rev-parse', 'HEAD')[1] if head else ''
    out = []
    for gid, g in games.items():
        g['bases'].sort(key=lambda e: (e['ext'] == '.nup', e['ext'] in ('.nsz', '.xcz'), e['rel']))   # un NSP/XCI plutôt que sa version compressée ou un NUP
        g['base'] = g['bases'][0] if g['bases'] else None
        g['updates'].sort(key=lambda e: e['ver'] or 0)
        prof = (profiles / gid).is_dir()
        first = g['base'] or (g['updates'] + g['dlc'])[0]
        name = (prof and profile_name(cfg, gid)) or clean_name(first['name']) or gid
        st = state.get(gid, {})
        builds = []
        for plat, b in st.get('builds', {}).items():
            moved = Path(cfg['outDir']) / Path(b['out']).name if cfg.get('outDir') else None
            if not Path(b['out']).exists() and moved and moved.exists():   # app rangée ailleurs puis dossier des apps changé
                b['out'] = str(moved); state_moved = True
            if Path(b['out']).exists():
                stale = bool(head and b.get('commit') and not same_commit(b['commit'], head))
                builds.append({**b, 'platform': plat, 'stale': stale,
                               'changes': game_changes(cfg, gid, name, b['commit'], head_full) if stale else None})
        save_dst = NX_SUPPORT / gid / 'save'
        out.append({
            'id': gid, 'name': name, 'hasBase': g['base'] is not None,
            'base': g['base'], 'otherBases': g['bases'][1:], 'updates': g['updates'], 'dlc': g['dlc'],
            'folder': str(Path(first['rel']).parent) if Path(first['rel']).parent != Path('.') else '',
            'profile': prof,
            'builds': builds,
            'icon': icon_url(gid),
            'save': {'exists': save_dst.is_dir() and any(save_dst.iterdir())},
            'envTxt': read_env_txt(gid),
            'shaders': shader_count(cfg, gid),
        })
    if state_moved:
        with LOCK:   # relu sous verrou : un build qui se termine écrit aussi state.json
            st2 = load_json(STATE, {})
            for gid, v in state.items():
                for plat, b in v.get('builds', {}).items():
                    cur = st2.get(gid, {}).get('builds', {}).get(plat)
                    if cur and cur.get('commit') == b.get('commit'):
                        cur['out'] = b['out']
            save_json(STATE, st2)
    out.sort(key=lambda g: (not g['profile'], g['name'].lower()))
    fetch_icons(cfg, [(g['id'], g['base']['path']) for g in out if not g['icon'] and g['base']])
    return out, orphans, archives


# ---------- catalogue : jeux qui ont un profil nxnative ----------
# Lu dans la branche distante (git fetch, sans toucher au clone : sans risque pendant un build), rafraîchi toutes les 6 h.
CATALOG = SUPPORT / 'catalog.json'
CATALOG_EVERY = 6 * 3600
REGIONAL = re.compile(r',\s+(Asian|Japanese|Korean|Chinese|European|American)\s+release$', re.I)
_catalog_lock = threading.Lock()


def build_catalog(cfg, fetch):
    with _catalog_lock:
        if fetch:
            git(cfg, 'fetch', '--quiet', timeout=300)
        code, ref = git(cfg, 'rev-parse', '@{u}')
        if code:
            code, ref = git(cfg, 'rev-parse', 'HEAD')
        if code:
            return None
        code, out = git(cfg, 'ls-tree', '--name-only', ref, 'dm/games/')
        ids = [l.split('/')[-1] for l in out.splitlines() if re.fullmatch(r'[0-9A-F]{16}', l.split('/')[-1])]
        r = subprocess.run(['git', '-C', cfg['nxnative'], 'cat-file', '--batch'], input=''.join(f'{ref}:dm/games/{i}/README.md\n' for i in ids).encode(),
                           capture_output=True, timeout=120)
        data, pos, names = r.stdout, 0, {}
        for i in ids:
            nl = data.index(b'\n', pos); head = data[pos:nl].split(); pos = nl + 1
            if len(head) == 3:
                size = int(head[2]); text = data[pos:pos + size].decode(errors='replace'); pos += size + 1
                m = re.match(r'#\s+(.+?)(?:\s+\(`.*)?$', text.split('\n', 1)[0])
                names[i] = m.group(1).strip() if m else i
            else:
                names[i] = i
        # Date d'arrivée de chaque profil (premier commit qui ajoute son README) : tri « plus récents / plus anciens ».
        added = {}
        code, log = git(cfg, 'log', '--format=@%ct', '--diff-filter=A', '--name-only', ref, '--', 'dm/games/*/README.md', timeout=120)
        t = 0
        for l in log.splitlines():
            if l.startswith('@'):
                t = int(l[1:])
            elif l.strip():
                added[l.split('/')[-2]] = t   # du plus récent au plus ancien : on garde le dernier vu, le premier ajout
        local = {p.name for p in (Path(cfg['nxnative']) / 'dm/games').iterdir()} if (Path(cfg['nxnative']) / 'dm/games').is_dir() else set()
        cat = {'fetched': time.time(), 'ref': ref[:7],
               'games': [{'id': i, 'name': names[i], 'new': i not in local, 'added': added.get(i)} for i in ids]}
        save_json(CATALOG, cat)
        return cat


def catalog_for(cfg, owned):
    """Jeux avec profil que l'on n'a pas. Une variante régionale (« …, Asian release ») est fusionnée avec le jeu principal."""
    cat = load_json(CATALOG, None)
    if cat and cat['games'] and 'added' not in cat['games'][0]:   # catalogue d'une version précédente : sans dates
        cat = build_catalog(cfg, fetch=False) or cat
    if not cat:
        return None
    owned_ids = {g['id'] for g in owned}
    owned_names = {g['name'].lower() for g in owned}
    main_names = {g['name'].lower() for g in cat['games'] if not REGIONAL.search(g['name'])}
    out = []
    for g in cat['games']:
        if g['id'] in owned_ids:
            continue
        base = REGIONAL.sub('', g['name']).lower()
        if REGIONAL.search(g['name']) and (base in main_names or base in owned_names):
            continue
        if g['name'].lower() in owned_names:
            continue
        out.append({**g, 'icon': icon_url(g['id'])})
    out.sort(key=lambda g: g['name'].lower())
    return {'fetched': cat['fetched'], 'ref': cat['ref'], 'games': out}


def catalog_loop():
    while True:
        try:
            cat = load_json(CATALOG, None)
            if not cat or time.time() - cat.get('fetched', 0) > CATALOG_EVERY:
                build_catalog(config(), fetch=True)
        except Exception:
            pass
        time.sleep(1800)


def read_env_txt(gid):
    p = NX_SUPPORT / gid / 'env.txt'
    try:
        return [l for l in p.read_text().splitlines() if l.strip() and not l.startswith('#')]
    except Exception:
        return []


# ---------- shaders partagés et réglages intégrés à l'app ----------
# Le jeu note chaque pipeline qu'il compile en jouant dans pso_user.tsv (lignes S/R/C, ordre de première utilisation), et
# chaque lancement suivant les compile tous avant la première image (« Preparing shaders »). Par défaut cette liste est dans
# ~/Library/Application Support/nxnative/<id>/, sur un seul Mac. Avec « shaders partagés », l'app la lit et l'écrit dans
# <dossier des apps>/nxnative-shaders/<id>/pso_user.tsv : un Mac compile d'avance ce que l'autre a déjà vu. Le cache compilé
# (pso_archive) reste sur chaque Mac (DM_PSO_ARCHIVE relatif au dossier du jeu, où le lanceur se place) : il est propre au
# GPU (M3 et M2 Pro n'ont pas le même). Les deux réglages vont dans l'app (Resources/dm/env.txt, lu par le lanceur après
# celui du Mac), avec les lignes du env.txt du jeu (DM_FPS_TARGET…) : ils suivent l'app sur un autre Mac.
SHADER_KEYS = ('DM_PSO_USER', 'DM_PSO_ARCHIVE')


def shared_list(cfg, gid):
    return Path(cfg['outDir']) / 'nxnative-shaders' / gid / 'pso_user.tsv'


def merge_lists(dst, *srcs):
    """Ajoute à dst les lignes des autres listes qu'il n'a pas encore, dans leur ordre. Renvoie le nombre de lignes ajoutées."""
    try:
        have = dst.read_text(errors='replace').splitlines() if dst.is_file() else []
    except OSError:
        return 0
    seen, new = set(have), []
    for s in srcs:
        try:
            lines = s.read_text(errors='replace').splitlines() if s and s.is_file() and s != dst else []
        except OSError:
            continue
        for l in lines:
            if l.strip() and not l.startswith('#') and l not in seen:
                seen.add(l); new.append(l)
    if new:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + '.tmp')
        tmp.write_text('\n'.join(have + new) + '\n'); tmp.replace(dst)
    return len(new)


def shader_count(cfg, gid):
    p = shared_list(cfg, gid) if cfg.get('sharedShaders', '1') != '0' else NX_SUPPORT / gid / 'pso_user.tsv'
    try:
        return sum(1 for l in p.read_text(errors='replace').splitlines() if l[:2] in ('S\t', 'R\t', 'C\t', 'M\t'))
    except OSError:
        return 0


def sync_lists(cfg, gid):
    """Réunit la liste de ce Mac et la liste partagée, dans le sens du réglage (rien n'est perdu en changeant d'avis)."""
    local, shared = NX_SUPPORT / gid / 'pso_user.tsv', shared_list(cfg, gid)
    if cfg.get('sharedShaders', '1') != '0':
        if Path(cfg['outDir']).is_dir():
            merge_lists(shared, local)
    elif shared.is_file():
        merge_lists(local, shared)


def app_env_lines(cfg, gid):
    lines = [l for l in read_env_txt(gid) if '=' in l and l.split('=', 1)[0].strip() not in SHADER_KEYS]
    if cfg.get('sharedShaders', '1') != '0' and cfg['outDir']:
        lines += [f'DM_PSO_USER={shared_list(cfg, gid)}', 'DM_PSO_ARCHIVE=pso_archive']
    return lines


def bundle_env(cfg, gid, app):
    """Met app_env_lines dans une app déjà construite et la re-signe (signature ad hoc seulement). Renvoie True si changée."""
    p = app / 'Contents/Resources/dm/env.txt'
    lines = app_env_lines(cfg, gid)
    text = ('# Écrit par Atelier NX (env.txt du jeu et shaders partagés)\n' + '\n'.join(lines) + '\n') if lines else ''
    try:
        cur = [l for l in p.read_text().splitlines() if l.strip() and not l.startswith('#')] if p.is_file() else []
    except OSError:
        cur = []
    if cur == lines or not p.parent.is_dir():   # le build l'a déjà mis (--env-file), sans le commentaire
        return False
    sig = subprocess.run(['codesign', '-dv', str(app)], capture_output=True, text=True).stderr
    if 'Signature=adhoc' not in sig:   # signée avec un vrai certificat : la re-signer en ad hoc la changerait
        raise ValueError(f"{app.name} n'a pas une signature ad hoc : non modifiée.")
    if text:
        p.write_text(text)
    else:
        p.unlink()
    r = subprocess.run(['codesign', '--force', '--sign', '-', str(app)], capture_output=True, text=True)
    if r.returncode:
        raise ValueError(f'{app.name} : signature impossible ({r.stderr.strip()[-200:]})')
    return True


def apply_shader_settings(cfg):
    """Pour chaque jeu déjà construit pour Mac : réunit les listes et met à jour l'env.txt intégré. (changées, erreurs)"""
    changed, errors = 0, []
    for gid, v in load_json(STATE, {}).items():
        b = v.get('builds', {}).get('mac-app')
        if not b or not Path(b['out']).exists():
            continue
        sync_lists(cfg, gid)
        try:
            changed += bundle_env(cfg, gid, Path(b['out']))
        except (OSError, ValueError) as e:
            errors.append(str(e))
    return changed, errors


def icon_url(gid):
    if (ICONS / f'{gid}.png').exists():
        return f'/icon/{gid}.png'
    if (ICONS / f'{gid}.jpg').exists():
        return f'/icon/{gid}.jpg'
    return None


def make_icon(gid, app):
    icns = Path(app) / 'Contents/Resources/AppIcon.icns'
    if icns.exists():
        ICONS.mkdir(parents=True, exist_ok=True)
        subprocess.run(['sips', '-s', 'format', 'png', '-Z', '256', str(icns), '--out', str(ICONS / f'{gid}.png')],
                       capture_output=True)


# ---------- réglages des apps Mac (choix enregistrés, lus par la fenêtre du jeu) ----------
# La fenêtre de réglages d'une app nxnative garde les choix dans les préférences de l'app (domaine = son bundle ID),
# clés « launcher.<VAR> » (chaînes) et « window.show » (booléen). Les écrire ici pré-remplit la fenêtre sans rien bloquer,
# contrairement à env.txt, qui l'emporte et grise le réglage (docs/frontends.md, Saved choices).

def mac_app(gid):
    b = load_json(STATE, {}).get(gid, {}).get('builds', {}).get('mac-app')
    return Path(b['out']) if b and Path(b['out']).exists() else None


def app_settings(app):
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    lj = load_json(app / 'Contents/Resources/dm/launcher.json', {})
    return info.get('CFBundleIdentifier'), lj.get('settings', [])


def read_prefs(bid):
    r = subprocess.run(['defaults', 'export', bid, '-'], capture_output=True)
    try:
        return plistlib.loads(r.stdout) if r.returncode == 0 else {}
    except Exception:
        return {}


def valid_value(setting, v):
    k = setting.get('kind')
    if k == 'choice':
        return v in [o[0] for o in setting.get('options', [])]
    if k == 'toggle':
        return v in (setting.get('on'), setting.get('off'))
    if k == 'range':
        try:
            return setting['min'] - 1e-9 <= float(v) <= setting['max'] + 1e-9
        except (TypeError, ValueError):
            return False
    return False


def write_prefs(bid, settings, values, show=None):
    """values : {VAR: valeur ou None}. Une valeur égale au défaut testé est retirée, comme le fait la fenêtre du jeu."""
    by = {s['env']: s for s in settings}
    for env, v in values.items():
        st = by.get(env)
        if not st:
            continue
        if v is None or str(v) == str(st.get('default')) or not valid_value(st, str(v)):
            subprocess.run(['defaults', 'delete', bid, f'launcher.{env}'], capture_output=True)
        else:
            subprocess.run(['defaults', 'write', bid, f'launcher.{env}', '-string', str(v)], capture_output=True)
    if show is not None:
        subprocess.run(['defaults', 'write', bid, 'window.show', '-bool', 'true' if show else 'false'], capture_output=True)


def game_prefs(gid):
    app = mac_app(gid)
    if not app:
        raise ValueError("Construis d'abord l'app Mac de ce jeu.")
    bid, settings = app_settings(app)
    prefs = read_prefs(bid)
    env = dict(l.split('=', 1) for l in read_env_txt(gid) if '=' in l)
    return {'bundleId': bid, 'show': prefs.get('window.show', True) is not False,
            'settings': [{**s, 'current': prefs.get(f"launcher.{s['env']}"), 'locked': env.get(s['env'])} for s in settings]}


def apply_defaults(cfg, gid, only_missing):
    """Réglages par défaut de l'atelier → choix enregistrés du jeu (seulement ceux que ce jeu propose)."""
    app = mac_app(gid)
    if not app:
        return 0
    bid, settings = app_settings(app)
    prefs = read_prefs(bid)
    d = cfg.get('gameDefaults') or {}
    vals = {}
    for st in settings:
        v = d.get(st['env'])
        if v in (None, '') or (only_missing and f"launcher.{st['env']}" in prefs):
            continue
        if valid_value(st, str(v)):
            vals[st['env']] = str(v)
    show = None
    if d.get('window.show') in ('0', '1') and not (only_missing and 'window.show' in prefs):
        show = d['window.show'] == '1'
    write_prefs(bid, settings, vals, show)
    return len(vals) + (show is not None)


# ---------- builds ----------

class Job:
    def __init__(self):
        self.proc = None
        self.info = None          # {'id', 'name', 'platform', 'language', 'started'}
        self.events = []          # journal du build en cours (ou du dernier)
        self.cond = threading.Condition()
        self.seq = 0
        self.result = None        # None pendant le build, puis {'ok', 'code', 'out'}
        self.cancelled = False
        self.item = None          # élément de la file en cours de construction

    def busy(self):
        return self.proc is not None and self.proc.poll() is None

    def push(self, ev):
        with self.cond:
            self.seq += 1
            ev['seq'] = self.seq
            self.events.append(ev)
            if len(self.events) > 4000:
                self.events = self.events[-3000:]
            self.cond.notify_all()


JOB = Job()


def stage_folder(cfg, game, update_ver, with_dlc):
    """Dossier de liens symboliques ne contenant qu'un jeu : le script attend un dossier par jeu.
    Le dossier <id>_build qu'il crée à côté y reste d'un build à l'autre (extraction réutilisée)."""
    d = Path(cfg['workDir']) / game['id']
    d.mkdir(parents=True, exist_ok=True)
    for p in d.iterdir():
        if p.is_symlink():
            p.unlink()
    picks = [game['base']]
    if game['base']['ext'] == '.nup':   # tout est dans le NUP ; nxnative refuse un NUP avec d'autres paquets à côté
        f = game['base']
        (d / f"base00 [{f['tid']}].nup").symlink_to(f['path'])
        return d, picks
    ups = game['updates']
    if update_ver == 'all':
        picks += ups                       # le script prend la plus récente
    elif update_ver != 'none':
        picks += [u for u in ups if str(u['ver']) == str(update_ver)]
    if with_dlc:
        picks += game['dlc']
    # Noms normalisés « [title ID][vN] » : nxnative reconnaît les mises à jour à cette étiquette, et les fichiers
    # d'origine (rangés n'importe où, nommés n'importe comment) restent intacts.
    for i, f in enumerate(picks):
        tag = f"[{f['tid']}]" + (f"[v{f['ver']}]" if f['ver'] is not None else '')
        (d / f"{f['kind']}{i:02d} {tag}{f['ext']}").symlink_to(f['path'])
    return d, picks


def safe_name(n):
    return re.sub(r'\s*[:/]\s*', ' - ', n).strip() or 'jeu'


# ---------- file de construction ----------
# Les builds s'enchaînent : « Construire » ajoute à la file (avec ses options), le suivant démarre à la fin du précédent,
# même en cas d'échec. La file est gardée sur disque ; quitter pendant un build la remet en tête et met la file en pause.
QUEUE_FILE = SUPPORT / 'queue.json'
QLOCK = threading.RLock()
OPT_KEYS = ('platform', 'language', 'update', 'dlc', 'noMotion', 'romfsSeparate', 'allowDamaged', 'seenShaders', 'shaderFiles', 'showFps')


def queue():
    q = load_json(QUEUE_FILE, {})
    return {'items': q.get('items', []), 'paused': q.get('paused', False), 'history': q.get('history', [])}


def save_queue(q):
    q['history'] = q['history'][-15:]
    save_json(QUEUE_FILE, q)


def enqueue(cfg, req):
    return enqueue_many(cfg, [req])[0]


def enqueue_many(cfg, reqs):
    """Ajoute des builds à la file, dans l'ordre donné ; renvoie la position de chacun."""
    games = {g['id']: g for g in scan(cfg)[0]}
    items = []
    for req in reqs:
        game = games.get(req.get('id'))
        if not game:
            raise ValueError(f"Jeu introuvable : {req.get('id')}")
        if not game['hasBase']:
            raise ValueError(f"{game['name']} : il manque le jeu de base (seulement une mise à jour ou des DLC).")
        item = {'id': game['id'], 'name': game['name'], **{k: req.get(k) for k in OPT_KEYS}}
        item['platform'] = item['platform'] or 'mac-app'
        item['language'] = item['language'] or cfg['language']
        item['update'] = item['update'] or 'all'
        item['dlc'] = True if item['dlc'] is None else item['dlc']
        items.append(item)
    out = []
    with QLOCK:
        q = queue()
        for item in items:
            same = next((i for i in q['items'] if i['id'] == item['id'] and i['platform'] == item['platform']), None)
            if same:                              # déjà dans la file : on met ses options à jour, sans changer sa place
                same.update(item); out.append(q['items'].index(same) + 1)
            else:
                item['qid'] = secrets.token_hex(4); item['added'] = time.time()
                q['items'].append(item); out.append(len(q['items']))
        save_queue(q)
    kick()
    return out


def prereq_step(msg, err=False):
    PREREQ['step'] = '' if err else msg


def kick():
    """Démarre l'élément suivant si rien ne tourne et que la file n'est pas en pause."""
    # Ce qui manque (outils, chaîne Metal…) est installé avant de prendre QLOCK : un téléchargement de plusieurs minutes
    # ne doit pas bloquer la page (ajout, ordre de la file). La page montre l'étape (PREREQ['step']).
    q = queue()
    if not JOB.busy() and not q['paused'] and q['items']:
        try:
            ensure_prereqs(config(), say=prereq_step, platform=q['items'][0].get('platform'), interactive=True)
        except PrereqError:   # tous les jeux échoueraient : on attend « Réessayer l'installation »
            with QLOCK:
                q = queue(); q['paused'] = True; save_queue(q)
            return
        finally:
            PREREQ['step'] = ''
    with QLOCK:
        while not JOB.busy():
            q = queue()
            if q['paused'] or not q['items']:
                return
            item = q['items'].pop(0); save_queue(q)
            try:
                start_build(config(), item)
                JOB.item = item   # sous QLOCK : finish_item (fil du build) attend que ce soit fait
                return
            except PrereqError:   # outils ou modules impossibles à installer : tous les jeux échoueraient, on attend
                q = queue(); q['items'].insert(0, item); q['paused'] = True; save_queue(q)
                return
            except Exception as e:
                q = queue()
                q['history'].append({**item, 'ok': False, 'error': str(e), 'ended': time.time()}); save_queue(q)


def finish_item(ok, code, cancelled, missing=None):
    with QLOCK:
        item, JOB.item = JOB.item, None
        if item and not ok and not cancelled and missing and not item.get('prereqRetry'):
            # échec sur un module ou un outil manquant que la vérification n'a pas vu : l'installer et relancer le jeu une fois
            try:
                if ensure_prereqs(config(), say=lambda m, err=False: JOB.push({'ev': 'say', 'msg': m, 'err': err}),
                                  force=True, extra=missing, platform=item.get('platform'), interactive=True):
                    q = queue(); q['items'].insert(0, {**item, 'prereqRetry': True}); save_queue(q)
                    item = None
            except PrereqError:
                q = queue(); q['paused'] = True; save_queue(q)
        if item:
            q = queue()
            q['history'].append({**item, 'ok': ok, 'code': code, 'cancelled': cancelled, 'ended': time.time(),
                                 'started': (JOB.info or {}).get('started')})
            save_queue(q)
    threading.Timer(1.0, kick).start()   # laisse la page recevoir la fin du build avant le suivant


def start_build(cfg, req):
    games, _, _ = scan(cfg)
    game = next((g for g in games if g['id'] == req.get('id')), None)
    if not game:
        raise ValueError('Jeu introuvable.')
    if not game['hasBase']:
        raise ValueError('Il manque le jeu de base (seulement une mise à jour ou des DLC).')
    nup = game['base']['ext'] == '.nup'
    if not nup and not Path(cfg['keys']).is_file():
        raise ValueError(f"prod.keys introuvable : {cfg['keys']}")
    if not Path(cfg['python']).exists():
        raise ValueError(f"Python introuvable : {cfg['python']}")
    platform = req.get('platform', 'mac-app')
    lang = req.get('language') or cfg['language']
    folder, picks = stage_folder(cfg, game, req.get('update', 'all'), req.get('dlc', True))
    Path(cfg['outDir']).mkdir(parents=True, exist_ok=True)
    out = Path(cfg['outDir']) / (safe_name(game['name']) + ('.app' if platform == 'mac-app' else '.ipa'))
    nice = ['nice', '-n', '10'] if cfg.get('lowPriority', '1') != '0' else []
    keys = ['-k', cfg['keys']] if Path(cfg['keys']).is_file() else []   # un NUP n'en a pas besoin
    cmd = nice + [cfg['python'], '-u', 'scripts/build_ipa_from_nsp.py', str(folder), *keys,
           '--progress', 'json', '--platform', platform, '-l', lang, '-o', str(out), '--title-id', game['id']]
    if platform == 'ios' and req.get('romfsSeparate'):
        cmd += ['--romfs', 'separate']
    if req.get('noMotion'):
        cmd += ['--no-motion']
    if req.get('allowDamaged'):
        cmd += ['--allow-damaged']
    if platform == 'mac-app' and req.get('seenShaders'):   # nxnative : seulement les shaders vus en test, minutes au lieu d'heures
        cmd += ['--seen-shaders']
    if platform == 'ios' and req.get('shaderFiles'):       # bibliothèque de shaders en fichiers séparés (AltServer : 2 Go max par fichier)
        cmd += ['--shader-files']
    if platform == 'ios' and req.get('showFps') is False:   # nxnative l'affiche par défaut sur iPhone
        cmd += ['--no-show-fps']
    if platform == 'mac-app':   # env.txt du jeu et shaders partagés, intégrés à l'app (Resources/dm/env.txt)
        sync_lists(cfg, game['id'])
        lines = app_env_lines(cfg, game['id'])
        envf = folder / 'atelier-env.txt'
        if lines:
            envf.write_text('\n'.join(lines) + '\n'); cmd += ['--env-file', str(envf)]
        else:
            envf.unlink(missing_ok=True)
    log = folder / 'atelier.log'
    with JOB.cond:
        JOB.events, JOB.result, JOB.cancelled = [], None, False
        JOB.info = {'id': game['id'], 'name': game['name'], 'platform': platform, 'language': lang,
                    'started': time.time(), 'out': str(out), 'log': str(log)}
    try:
        ensure_prereqs(cfg, say=lambda m, err=False: JOB.push({'ev': 'say', 'msg': m, 'err': err}),
                       platform=platform, interactive=True)
    except PrereqError:
        with JOB.cond:
            JOB.result = {'ok': False, 'code': None, 'out': str(out), 'cancelled': False}
        JOB.push({'ev': 'done', 'ok': False, 'code': None, 'cancelled': False})
        raise
    head = git(cfg, 'rev-parse', '--short', 'HEAD')[1]
    JOB.push({'ev': 'say', 'msg': f"Fichiers utilisés : " + ', '.join(p['name'] for p in picks)})
    JOB.push({'ev': 'say', 'msg': f"nxnative {head} · {platform} · langue {lang}" + (' · priorité basse' if nice else '')})
    JOB.proc = subprocess.Popen(cmd, cwd=cfg['nxnative'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, env=tool_env(), start_new_session=True)
    # iPhone, fichiers du jeu à part : l'image RomFS à copier sur le téléphone (push_romfs.sh) reste dans le dossier de build
    keep = platform == 'ios' and bool(req.get('romfsSeparate'))
    threading.Thread(target=pump, args=(JOB.proc, log, cfg, game, platform, lang, str(out), head, keep), daemon=True).start()


def pump(proc, log, cfg, game, platform, lang, out, head, keep_build=False):
    lf = open(log, 'a', encoding='utf-8')
    lf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(proc.args)}\n")

    def read_err():
        for line in proc.stderr:
            lf.write('[stderr] ' + line); lf.flush()
            line = line.rstrip()
            if line:
                JOB.push({'ev': 'say', 'msg': line, 'err': True})
    t = threading.Thread(target=read_err, daemon=True); t.start()
    missing = None
    for line in proc.stdout:
        lf.write(line); lf.flush()
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            ev = {'ev': 'say', 'msg': line}
        m = MISSING_RE.search(str(ev.get('msg', ''))) if ev.get('ev') == 'say' else None
        if m:
            try:
                missing = ([x.strip() for x in m.group(1).split(',') if x.strip()], shlex.split(m.group(2) or ''))
            except ValueError:
                missing = ([x.strip() for x in m.group(1).split(',') if x.strip()], [])
        elif ev.get('ev') == 'say' and TOOLS_RE.search(str(ev.get('msg', ''))):
            missing = missing or ([], [])   # outil du Mac : ensure_system le réinstalle
        JOB.push(ev)
    code = proc.wait(); t.join(timeout=5)
    ok = code == 0 and Path(out).exists()
    if ok:
        with LOCK:
            st = load_json(STATE, {})
            st.setdefault(game['id'], {}).setdefault('builds', {})[platform] = {
                'out': out, 'language': lang, 'commit': head, 'built': time.strftime('%Y-%m-%d %H:%M')}
            save_json(STATE, st)
        if platform == 'mac-app':
            try:   # rien ici ne doit bloquer la fin du build (et donc la file)
                make_icon(game['id'], out)
                n = apply_defaults(cfg, game['id'], only_missing=True)
                if n:
                    JOB.push({'ev': 'say', 'msg': f"{n} réglage(s) par défaut pré-rempli(s) dans la fenêtre du jeu"})
            except Exception as e:
                JOB.push({'ev': 'say', 'msg': f'Après le build : {e}', 'err': True})
        what = 'build' if cfg.get('cleanAfterBuild') == '1' and not keep_build else 'obsolete' if cfg.get('cleanObsolete', '1') != '0' else None
        if cfg.get('cleanAfterBuild') == '1' and keep_build:
            JOB.push({'ev': 'say', 'msg': "Fichiers de build gardés : l'image du jeu à copier sur l'iPhone (fichiers à part) y est."})
        if what:
            try:
                freed = clean_build(cfg, game['id'], what)
                if freed:
                    label = 'Fichiers de build supprimés' if what == 'build' else "Fichiers que nxnative n'utilise plus supprimés"
                    size = f"{freed / 1e9:.1f} Go".replace('.', ',') if freed >= 1e9 else f"{max(1, round(freed / 1e6))} Mo"
                    JOB.push({'ev': 'say', 'msg': f"{label} : {size} libérés"})
            except Exception as e:
                JOB.push({'ev': 'say', 'msg': f'Nettoyage du dossier obsolete : {e}', 'err': True})
    lf.write(f"===== fin, code {code}\n"); lf.close()
    with JOB.cond:
        JOB.result = {'ok': ok, 'code': code, 'out': out, 'cancelled': JOB.cancelled}
    JOB.push({'ev': 'done', 'ok': ok, 'code': code, 'cancelled': JOB.cancelled})
    finish_item(ok, code, JOB.cancelled, missing)


# ---------- place disque des fichiers de build ----------
# <dossier de travail>/<title ID>/ : liens vers les fichiers du jeu, et <title ID>_build (extraction, shaders, archives
# de pipelines) que nxnative réutilise d'un build à l'autre. Il range ce qu'il n'utilise plus dans <…>_build/obsolete
# (et le jeu Mac lui-même, pour les archives de pipelines remplacées) ; il ne le vide qu'au build réussi suivant.
def build_dir(cfg, gid):
    if not re.fullmatch(r'[0-9A-F]{16}', gid or ''):
        raise ValueError('identifiant invalide')
    return Path(cfg['workDir']) / gid


def obsolete_dirs(d):
    return [p for p in (d / 'obsolete', *d.glob('*_build/obsolete')) if p.is_dir() and not p.is_symlink()]


def du(p):
    """Taille sur le disque en octets (du ne suit pas les liens : les fichiers du jeu ne comptent pas)."""
    r = subprocess.run(['du', '-sk', str(p)], capture_output=True, text=True)
    try:
        return int(r.stdout.split()[0]) * 1024
    except (IndexError, ValueError):
        return 0


def build_space(cfg, gid):
    d = build_dir(cfg, gid)
    if not d.is_dir():
        return {'total': 0, 'obsolete': 0}
    return {'total': du(d), 'obsolete': sum(du(o) for o in obsolete_dirs(d))}


def space_all(cfg):
    root = Path(cfg['workDir'])
    names = {g['id']: g['name'] for g in scan(cfg)[0]}
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir() and re.fullmatch(r'[0-9A-F]{16}', d.name):
                out.append({'id': d.name, 'name': names.get(d.name, ''), **build_space(cfg, d.name)})
    out.sort(key=lambda e: -e['total'])
    return out


def clean_build(cfg, gid, what):
    """what : 'obsolete' (ce que nxnative n'utilise plus), 'all' (tout le dossier : le prochain build réextrait le jeu)
    ou 'build' (comme 'all' en gardant le journal de build atelier.log, pour la suppression automatique après un build).
    Renvoie les octets libérés. Les liens vers les fichiers du jeu sont retirés, jamais suivis."""
    d = build_dir(cfg, gid)
    if JOB.busy() and (JOB.info or {}).get('id') == gid:
        raise ValueError('Ce jeu est en cours de construction.')
    if not d.is_dir():
        return 0
    targets = (obsolete_dirs(d) if what == 'obsolete' else [d] if what == 'all'
               else [p for p in d.iterdir() if p.name != 'atelier.log'] if what == 'build' else [])
    freed = 0
    for t in targets:
        if t.is_symlink() or t.is_file():   # liens vers les fichiers du jeu : retirés, jamais suivis
            freed += 0 if t.is_symlink() else t.stat().st_blocks * 512
            t.unlink(); continue
        n = du(t)
        shutil.rmtree(t, ignore_errors=True)
        freed += n - (du(t) if t.exists() else 0)
    return freed


UNZIPPING = set()


def unzip(cfg, path):
    p = Path(path).resolve()
    rom = Path(cfg['romDir']).resolve()
    if rom not in p.parents or p.suffix.lower() != '.zip' or not p.is_file():
        raise ValueError('Archive refusée.')
    if str(p) in UNZIPPING:
        return

    def run():
        UNZIPPING.add(str(p))
        try:
            subprocess.run(['ditto', '-x', '-k', str(p), str(p.parent)], capture_output=True)
        finally:
            UNZIPPING.discard(str(p))
    threading.Thread(target=run, daemon=True).start()


def cancel_build():
    if JOB.busy():
        JOB.cancelled = True
        try:
            os.killpg(JOB.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        threading.Timer(8, lambda: JOB.busy() and os.killpg(JOB.proc.pid, signal.SIGKILL)).start()


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = 'AtelierNX'

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype='application/json; charset=utf-8'):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self):
        return self.headers.get('Host', '').split(':')[0] in ('127.0.0.1', 'localhost')

    def authed(self, qs=None):
        tok = self.headers.get('X-Token') or (qs or {}).get('token', [''])[0]
        return self.host_ok() and secrets.compare_digest(tok, TOKEN)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n) or b'{}')

    def do_GET(self):
        u = urlparse(self.path); qs = parse_qs(u.query)
        if not self.host_ok():
            return self.send(403, {'error': 'hôte refusé'})
        if u.path in ('/', '/index.html'):
            html = (HERE / 'index.html').read_text().replace('__TOKEN__', TOKEN)
            return self.send(200, html.encode(), 'text/html; charset=utf-8')
        m = re.fullmatch(r'/icon/([0-9A-F]{16})\.(png|jpg)', u.path)
        if m:
            p = ICONS / f'{m.group(1)}.{m.group(2)}'
            if p.exists():
                return self.send(200, p.read_bytes(), 'image/png' if m.group(2) == 'png' else 'image/jpeg')
            return self.send(404, {})
        if not self.authed(qs):
            return self.send(403, {'error': 'jeton invalide'})
        cfg = config()
        if u.path == '/api/state':
            games, orphans, archives = scan(cfg)
            catalog = catalog_for(cfg, games)
            nx_ok = nx_present(cfg) and not NX_INSTALL['running']
            head = git(cfg, 'log', '-1', '--format=%h|%cd', '--date=format:%d/%m %H:%M')[1] if nx_ok else ''
            return self.send(200, {
                'config': cfg, 'games': games, 'orphans': orphans, 'archives': archives, 'unzipping': sorted(UNZIPPING), 'catalog': catalog,
                'rom': bool(cfg['romDir']) and Path(cfg['romDir']).is_dir(), 'keys': Path(cfg['keys']).is_file(),
                'nxnative': {'ok': nx_ok, 'head': head, 'path': cfg['nxnative'], 'install': NX_INSTALL},
                'prereqs': {'missing': PREREQ['missing'], 'error': PREREQ['error'], 'step': PREREQ['step']},
                'job': {'busy': JOB.busy(), 'info': JOB.info, 'result': JOB.result, 'item': JOB.item},
                'queue': queue(),
            })
        if u.path == '/api/space':
            gid = qs.get('id', [''])[0]
            return self.send(200, build_space(cfg, gid) if gid else {'games': space_all(cfg)})
        if u.path == '/api/busy':
            return self.send(200, {'busy': JOB.busy(), 'queued': len(queue()['items'])})
        if u.path == '/api/events':
            return self.stream(int(qs.get('since', ['0'])[0]))
        return self.send(404, {'error': 'inconnu'})

    def stream(self, since):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            while True:
                with JOB.cond:
                    batch = [e for e in JOB.events if e['seq'] > since]
                    if not batch:
                        JOB.cond.wait(15)
                        batch = [e for e in JOB.events if e['seq'] > since]
                if batch:
                    since = batch[-1]['seq']
                    for e in batch:
                        self.wfile.write(f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode())
                else:
                    self.wfile.write(b': ping\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        u = urlparse(self.path)
        if not self.authed():
            return self.send(403, {'error': 'jeton invalide'})
        cfg = config()
        try:
            req = self.body()
            if u.path == '/api/build':
                return self.send(200, {'ok': True, 'position': enqueue(cfg, req)})
            if u.path == '/api/build/batch':
                pos = enqueue_many(cfg, req.get('items') or [])
                return self.send(200, {'ok': True, 'positions': pos})
            if u.path == '/api/queue/remove':
                with QLOCK:
                    q = queue(); q['items'] = [i for i in q['items'] if i['qid'] != req.get('qid')]; save_queue(q)
                return self.send(200, {'ok': True})
            if u.path == '/api/queue/move':
                with QLOCK:
                    q = queue(); it = q['items']
                    i = next((n for n, x in enumerate(it) if x['qid'] == req.get('qid')), None)
                    j = None if i is None else max(0, min(len(it) - 1, i + int(req.get('delta', 0))))
                    if i is not None:
                        it.insert(j, it.pop(i)); save_queue(q)
                return self.send(200, {'ok': True})
            if u.path == '/api/queue/pause':
                with QLOCK:
                    q = queue(); q['paused'] = bool(req.get('paused')); save_queue(q)
                kick(); return self.send(200, {'ok': True})
            if u.path == '/api/queue/clear-history':
                with QLOCK:
                    q = queue(); q['history'] = []; save_queue(q)
                return self.send(200, {'ok': True})
            if u.path == '/api/space/clean':
                if req.get('id') == '*':   # tous les jeux, sauf celui en cours de build
                    busy = (JOB.info or {}).get('id') if JOB.busy() else None
                    ids = [i['id'] for i in space_all(cfg) if i['id'] != busy]
                else:
                    ids = [req.get('id')]
                freed = sum(clean_build(cfg, gid, req.get('what')) for gid in ids)
                return self.send(200, {'ok': True, 'freed': freed})
            if u.path == '/api/unzip':
                unzip(cfg, req.get('path', '')); return self.send(200, {'ok': True})
            if u.path == '/api/cancel':
                cancel_build(); return self.send(200, {'ok': True})
            if u.path == '/api/config':
                cur = load_json(CONFIG, {})
                for k in DEFAULTS:
                    if k in req and isinstance(req[k], str):
                        cur[k] = req[k].strip()
                if isinstance(req.get('gameDefaults'), dict):
                    cur['gameDefaults'] = {k: str(v) for k, v in req['gameDefaults'].items()
                                           if re.fullmatch(r'DM_[A-Z0-9_]+|window\.show', k) and v not in (None, '')}
                old = {**DEFAULTS, **load_json(CONFIG, {})}
                shared_changed = any(cur.get(k, DEFAULTS[k]) != old[k] for k in ('sharedShaders', 'outDir'))
                save_json(CONFIG, cur)
                if shared_changed:   # les apps déjà construites suivent le réglage
                    threading.Thread(target=lambda: _quiet(apply_shader_settings, config()), daemon=True).start()
                return self.send(200, {'ok': True})
            if u.path == '/api/choose':
                kind = req.get('kind', 'folder')
                script = ('POSIX path of (choose folder with prompt "Choisir un dossier")' if kind == 'folder'
                          else 'POSIX path of (choose file with prompt "Choisir un fichier")')
                r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
                return self.send(200, {'path': r.stdout.strip().rstrip('/') if r.returncode == 0 else None})
            if u.path == '/api/open':
                gid, plat = req.get('id'), req.get('platform', 'mac-app')
                b = load_json(STATE, {}).get(gid, {}).get('builds', {}).get(plat)
                target = b['out'] if b else None
                if req.get('what') == 'log':
                    target = str(Path.home() / f'Library/Logs/nxnative/{gid}.log')
                elif req.get('what') == 'buildlog':
                    target = str(Path(cfg['workDir']) / gid / 'atelier.log')
                elif req.get('what') == 'data':
                    target = str(NX_SUPPORT / gid)
                elif req.get('what') == 'save':
                    target = str(NX_SUPPORT / gid / 'save')
                if not target or not Path(target).exists():
                    return self.send(404, {'error': 'Introuvable.'})
                if req.get('what') in ('log', 'buildlog'):
                    args = ['open', '-a', 'Console', target]
                elif req.get('reveal'):
                    args = ['open', '-R', target]
                else:
                    args = ['open', target]
                subprocess.Popen(args); return self.send(200, {'ok': True})
            if u.path == '/api/prefs/get':
                return self.send(200, game_prefs(req['id']))
            if u.path == '/api/prefs/set':
                app = mac_app(req['id'])
                if not app:
                    return self.send(404, {'error': 'App introuvable.'})
                bid, settings = app_settings(app)
                write_prefs(bid, settings, req.get('values', {}), req.get('show'))
                return self.send(200, {'ok': True})
            if u.path == '/api/prefs/defaults':
                n = apply_defaults(cfg, req['id'], only_missing=False)
                return self.send(200, {'ok': True, 'n': n})
            if u.path == '/api/env':
                gid = req['id']
                p = NX_SUPPORT / gid / 'env.txt'
                lines = [l.strip() for l in req.get('lines', []) if re.fullmatch(r'[A-Z0-9_]+=.*', l.strip())]
                if lines:
                    p.parent.mkdir(parents=True, exist_ok=True); p.write_text('\n'.join(lines) + '\n')
                elif p.exists():
                    p.unlink()
                app, note = mac_app(gid), ''
                if app:   # l'app emporte ces réglages sur un autre Mac
                    try:
                        bundle_env(cfg, gid, app)
                    except (OSError, ValueError) as e:
                        note = str(e)
                return self.send(200, {'ok': True, 'note': note})
            if u.path == '/api/shaders/apply':
                changed, errors = apply_shader_settings(cfg)
                return self.send(200, {'ok': not errors, 'changed': changed, 'errors': errors})
            if u.path in ('/api/nx/check', '/api/nx/pull') and not nx_present(cfg):   # dossier vide ou absent : pas un clone
                return self.send(200, {'ok': False, 'missing': True, 'msg': f"nxnative n'est pas installé dans {cfg['nxnative']}."})
            if u.path == '/api/nx/check':
                code, outp = git(cfg, 'fetch', '--quiet')
                if code:
                    return self.send(200, {'ok': False, 'msg': outp})
                n = git(cfg, 'rev-list', '--count', 'HEAD..@{u}')[1]
                logs = git(cfg, 'log', '--format=%h  %cd  %s', '--date=format:%d/%m %H:%M', '-15', 'HEAD..@{u}')[1]
                return self.send(200, {'ok': True, 'behind': int(n or 0), 'log': logs})
            if u.path == '/api/catalog/refresh':
                build_catalog(cfg, fetch=True); return self.send(200, {'ok': True})
            if u.path == '/api/profile-page':
                gid = req.get('id', '')
                if not re.fullmatch(r'[0-9A-F]{16}', gid):
                    return self.send(400, {'error': 'identifiant invalide'})
                subprocess.Popen(['open', f'https://github.com/seedds/nxnative/tree/main/dm/games/{gid}'])
                return self.send(200, {'ok': True})
            if u.path == '/api/commit-page':
                h = req.get('h', '')
                if not re.fullmatch(r'[0-9a-f]{7,40}', h):
                    return self.send(400, {'error': 'commit invalide'})
                subprocess.Popen(['open', f'https://github.com/seedds/nxnative/commit/{h}'])
                return self.send(200, {'ok': True})
            if u.path == '/api/nx/pull':
                if JOB.busy():
                    return self.send(409, {'error': 'Attends la fin du build en cours.'})
                code, outp = git(cfg, 'pull', '--ff-only', '--autostash', timeout=600)
                build_catalog(cfg, fetch=False)   # les « nouveau » disparaissent une fois le clone à jour
                try:
                    installed = ensure_prereqs(cfg)   # la nouvelle version peut demander un module de plus
                    mods = f"Modules Python installés : {', '.join(installed)}." if installed else ''
                except PrereqError as e:
                    mods = str(e)
                return self.send(200, {'ok': code == 0, 'msg': outp, 'modules': mods})
            if u.path == '/api/nx/install':
                start_nx_install(); return self.send(200, {'ok': True})
            if u.path == '/api/prereqs/install':
                ios = any(i.get('platform') == 'ios' for i in queue()['items'])
                OPENED.clear()   # clic explicite : rouvrir Terminal ou l'App Store si c'est encore nécessaire
                try:
                    installed = ensure_prereqs(cfg, say=prereq_step, force=True, platform='ios' if ios else None, interactive=True)
                except PrereqError as e:
                    return self.send(200, {'ok': False, 'msg': str(e)})
                finally:
                    PREREQ['step'] = ''
                with QLOCK:
                    q = queue(); q['paused'] = False; save_queue(q)
                kick()
                return self.send(200, {'ok': True, 'installed': installed})
            return self.send(404, {'error': 'inconnu'})
        except Exception as e:
            return self.send(400, {'error': str(e)})


def _quiet(f, *a):
    try:
        f(*a)
    except Exception:
        pass   # l'erreur reste dans PREREQ et s'affiche dans la page


def migrate_workdir(cfg):
    """1.0.3 : dossier de build renommé « ….noindex » (Spotlight l'ignore). Même disque : renommage instantané."""
    if not cfg['workDir']:
        return
    new = Path(cfg['workDir'])
    old = new.with_suffix('')
    if new.suffix == '.noindex' and not new.exists() and old.is_dir():
        try:
            old.rename(new)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=0)
    ap.add_argument('--open', action='store_true', help='ouvrir la page dans le navigateur')
    a = ap.parse_args()
    srv = ThreadingHTTPServer(('127.0.0.1', a.port), Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]

    def stop(*_):
        if JOB.busy() and JOB.item:              # quitter pendant un build : il reprendra en tête de file, file en pause
            with QLOCK:
                q = queue(); q['items'].insert(0, JOB.item); q['paused'] = True; save_queue(q)
        cancel_build()
        try:
            if load_json(SUPPORT / 'server.json', {}).get('pid') == os.getpid():
                (SUPPORT / 'server.json').unlink()
        except OSError:
            pass
        os._exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    # Permet à l'app de réutiliser un serveur déjà lancé (et son build en cours) au lieu d'en démarrer un second.
    save_json(SUPPORT / 'server.json', {'port': port, 'pid': os.getpid()})
    migrate_workdir(config())
    threading.Thread(target=catalog_loop, daemon=True).start()
    threading.Thread(target=lambda: _quiet(apply_shader_settings, config()), daemon=True).start()   # listes réunies, apps à jour
    if nx_present(config()):   # sinon la page propose d'installer nxnative (qui installe aussi les modules)
        threading.Thread(target=lambda: _quiet(ensure_prereqs, config()), daemon=True).start()   # vérifié dès le lancement
    threading.Timer(2.0, kick).start()         # reprend une file laissée non vide (sauf si elle est en pause)
    print(f'PORT {port}', flush=True)
    if a.open:
        webbrowser.open(f'http://127.0.0.1:{port}/')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        stop()


if __name__ == '__main__':
    main()
