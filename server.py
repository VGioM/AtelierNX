#!/usr/bin/env python3
"""Atelier NX : serveur local qui pilote nxnative (seedds/nxnative) pour l'interface HTML.

Écoute sur 127.0.0.1 uniquement ; chaque requête /api doit porter le jeton injecté dans la page.
Lancé par Atelier NX.app (ou à la main : python3 server.py --open).
"""
import json, os, plistlib, re, secrets, shutil, signal, struct, subprocess, sys, threading, time, argparse, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
SUPPORT = Path(os.environ.get('ATELIERNX_SUPPORT') or Path.home() / 'Library/Application Support/AtelierNX')   # autre dossier : tests
CONFIG = SUPPORT / 'config.json'
STATE = SUPPORT / 'state.json'
ICONS = SUPPORT / 'icons'
ARTWORK = Path.home() / 'Library/Application Support/NS Port Builder/artwork'
NX_SUPPORT = Path.home() / 'Library/Application Support/nxnative'

# Chemins vides = à choisir dans Réglages ; les dossiers d'apps, de build et d'anciennes sauvegardes
# sont alors pris dans le dossier des jeux (voir config()).
DEFAULTS = {
    'nxnative': str(Path.home() / 'nxnative'),
    'python': '',
    'romDir': '',
    'keys': str(Path.home() / '.switch/prod.keys'),
    'outDir': '',
    'workDir': '',   # vide = <jeux>/nxnative-build.noindex (« .noindex » : Spotlight n'indexe pas les milliers de fichiers de build)
    'oldSaveDir': '',
    'language': 'fr',
    'lowPriority': '1',   # builds avec nice : ils cèdent le processeur aux autres apps
    'gameDefaults': {},   # {DM_…: valeur, 'window.show': '0'/'1'} pré-remplis à chaque nouveau build Mac
}
GAME_EXT = {'.nsp', '.xci', '.nsz', '.xcz'}
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


def find_python(nxnative):
    """Premier Python 3.12+ trouvé : venv du clone nxnative, puis Homebrew, python.org, /usr/local."""
    cands = [Path(nxnative) / '.venv/bin/python', Path(nxnative) / 'venv/bin/python']
    for d in ('/opt/homebrew/bin', '/Library/Frameworks/Python.framework/Versions/Current/bin', '/usr/local/bin'):
        cands += [Path(d) / f'python3.{m}' for m in range(20, 11, -1)]
    return next((str(c) for c in cands if c.exists()), 'python3')


def config():
    cfg = {**DEFAULTS, **load_json(CONFIG, {})}
    rom = cfg['romDir']
    for k, sub in (('outDir', 'Apps'), ('workDir', 'nxnative-build.noindex'), ('oldSaveDir', 'save')):
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


def profile_name(cfg, gid):
    """Nom officiel du jeu, lu dans le titre du README de son profil nxnative."""
    try:
        first = (Path(cfg['nxnative']) / 'dm/games' / gid / 'README.md').read_text().splitlines()[0]
        m = re.match(r'#\s+(.+?)(?:\s+\(`.*)?$', first)
        return m.group(1).strip() if m else None
    except (OSError, IndexError):
        return None


def header_key(cfg):
    try:
        for line in Path(cfg['keys']).read_text().splitlines():
            k, _, v = line.replace(' ', '').partition('=')
            if k == 'header_key':
                return bytes.fromhex(v)
    except (OSError, ValueError):
        pass
    return None


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


def version_from_name(name):
    for pat in (r'\[v(\d+)\]', r'\(v(\d+)\)', r'\[(\d{5,})\]'):
        m = re.search(pat, name)
        if m and int(m.group(1)) % 65536 == 0:
            return int(m.group(1))
    return None


def identify(cfg, path, cache, hk):
    st = path.stat()
    key = f'{path}|{st.st_size}|{int(st.st_mtime)}'
    if key in cache:
        return cache[key]
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


def scan(cfg):
    if not cfg['romDir']:
        return [], [], []
    rom = Path(cfg['romDir'])
    cache = load_json(IDS_CACHE, {})
    n0 = len(cache)
    hk = header_key(cfg)
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
            orphans.append({'path': str(f), 'name': f.name, 'rel': rel, 'why': 'identifiant illisible'})
            continue
        tid = int(r['tid'], 16)
        low = tid & 0xFFF
        if low == 0:
            kind, base = 'base', tid
        elif low == 0x800:
            kind, base = 'update', tid & ~0xFFF
        else:
            kind, base = 'dlc', (tid & ~0xFFF) - 0x1000
        e = {'path': str(f), 'name': f.name, 'rel': rel, 'kind': kind, 'tid': r['tid'],
             'ver': 0 if kind == 'base' else r['ver'], 'size': size, 'ext': ext}
        g = games.setdefault(f'{base:016X}', {'base': None, 'bases': [], 'updates': [], 'dlc': []})
        if kind == 'base':
            g['bases'].append(e)
        elif kind == 'update':
            g['updates'].append(e)
        else:
            g['dlc'].append(e)
    if len(cache) != n0:
        save_json(IDS_CACHE, cache)
    state = load_json(STATE, {})
    profiles = Path(cfg['nxnative']) / 'dm/games'
    head = git(cfg, 'rev-parse', '--short', 'HEAD')[1] if (Path(cfg['nxnative']) / '.git').exists() else ''
    out = []
    for gid, g in games.items():
        g['bases'].sort(key=lambda e: (e['ext'] in ('.nsz', '.xcz'), e['rel']))   # un NSP/XCI plutôt que sa version compressée
        g['base'] = g['bases'][0] if g['bases'] else None
        g['updates'].sort(key=lambda e: e['ver'] or 0)
        prof = (profiles / gid).is_dir()
        first = g['base'] or (g['updates'] + g['dlc'])[0]
        name = (prof and profile_name(cfg, gid)) or clean_name(first['name']) or gid
        st = state.get(gid, {})
        builds = []
        for plat, b in st.get('builds', {}).items():
            if Path(b['out']).exists():
                builds.append({**b, 'platform': plat, 'stale': bool(head and b.get('commit') and b['commit'] != head)})
        save_dst = NX_SUPPORT / gid / 'save'
        old_save = Path(cfg['oldSaveDir']) / gid
        out.append({
            'id': gid, 'name': name, 'hasBase': g['base'] is not None,
            'base': g['base'], 'otherBases': g['bases'][1:], 'updates': g['updates'], 'dlc': g['dlc'],
            'folder': str(Path(first['rel']).parent) if Path(first['rel']).parent != Path('.') else '',
            'profile': prof,
            'builds': builds,
            'icon': icon_url(gid),
            'save': {'exists': save_dst.is_dir() and any(save_dst.iterdir()),
                     'old': old_save.is_dir() and any(p for p in old_save.iterdir() if not p.name.startswith('.'))},
            'envTxt': read_env_txt(gid),
        })
    out.sort(key=lambda g: (not g['profile'], g['name'].lower()))
    fetch_banners([g['id'] for g in out if not g['icon']])
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
        local = {p.name for p in (Path(cfg['nxnative']) / 'dm/games').iterdir()} if (Path(cfg['nxnative']) / 'dm/games').is_dir() else set()
        cat = {'fetched': time.time(), 'ref': ref[:7],
               'games': [{'id': i, 'name': names[i], 'new': i not in local} for i in ids]}
        save_json(CATALOG, cat)
        return cat


def catalog_for(cfg, owned):
    """Jeux avec profil que l'on n'a pas. Une variante régionale (« …, Asian release ») est fusionnée avec le jeu principal."""
    cat = load_json(CATALOG, None)
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
    fetch_banners([g['id'] for g in out if not g['icon']])
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


_banner_lock = threading.Lock()
_banner_tried = set()


def fetch_banners(ids):
    """Jaquettes manquantes : image eShop listée dans l'index de NX2Apple (téléchargée une fois, en arrière-plan)."""
    todo = [i for i in ids if i not in _banner_tried]
    if not todo:
        return
    _banner_tried.update(todo)
    idx = load_json(ARTWORK / 'index.json', {}).get('banners', {})

    def run():
        import urllib.request
        with _banner_lock:
            for gid in todo:
                url = idx.get(gid)
                if not url or urlparse(url).hostname != 'img-eshop.cdn.nintendo.net':
                    continue
                try:
                    with urllib.request.urlopen(url, timeout=20) as r:
                        data = r.read(5_000_000)
                    ICONS.mkdir(parents=True, exist_ok=True)
                    (ICONS / f'{gid}.jpg').write_bytes(data)
                except Exception:
                    pass
    threading.Thread(target=run, daemon=True).start()


def read_env_txt(gid):
    p = NX_SUPPORT / gid / 'env.txt'
    try:
        return [l for l in p.read_text().splitlines() if l.strip() and not l.startswith('#')]
    except Exception:
        return []


def icon_url(gid):
    if (ICONS / f'{gid}.png').exists():
        return f'/icon/{gid}.png'
    if (ICONS / f'{gid}.jpg').exists():
        return f'/icon/{gid}.jpg'
    if (ARTWORK / f'{gid}.jpg').exists():
        return f'/art/{gid}.jpg'
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
OPT_KEYS = ('platform', 'language', 'update', 'dlc', 'noMotion', 'romfsSeparate', 'allowDamaged')


def queue():
    q = load_json(QUEUE_FILE, {})
    return {'items': q.get('items', []), 'paused': q.get('paused', False), 'history': q.get('history', [])}


def save_queue(q):
    q['history'] = q['history'][-15:]
    save_json(QUEUE_FILE, q)


def enqueue(cfg, req):
    games, _, _ = scan(cfg)
    game = next((g for g in games if g['id'] == req.get('id')), None)
    if not game:
        raise ValueError('Jeu introuvable.')
    if not game['hasBase']:
        raise ValueError('Il manque le jeu de base (seulement une mise à jour ou des DLC).')
    item = {'id': game['id'], 'name': game['name'], **{k: req.get(k) for k in OPT_KEYS}}
    item['platform'] = item['platform'] or 'mac-app'
    item['language'] = item['language'] or cfg['language']
    with QLOCK:
        q = queue()
        same = next((i for i in q['items'] if i['id'] == item['id'] and i['platform'] == item['platform']), None)
        if same:                                  # déjà dans la file : on met ses options à jour, sans changer sa place
            same.update(item); pos = q['items'].index(same) + 1
        else:
            item['qid'] = secrets.token_hex(4); item['added'] = time.time()
            q['items'].append(item); pos = len(q['items'])
        save_queue(q)
    kick()
    return pos


def kick():
    """Démarre l'élément suivant si rien ne tourne et que la file n'est pas en pause."""
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
            except Exception as e:
                q = queue()
                q['history'].append({**item, 'ok': False, 'error': str(e), 'ended': time.time()}); save_queue(q)


def finish_item(ok, code, cancelled):
    with QLOCK:
        item, JOB.item = JOB.item, None
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
    if not Path(cfg['keys']).is_file():
        raise ValueError(f"prod.keys introuvable : {cfg['keys']}")
    if not Path(cfg['python']).exists():
        raise ValueError(f"Python introuvable : {cfg['python']}")
    platform = req.get('platform', 'mac-app')
    lang = req.get('language') or cfg['language']
    folder, picks = stage_folder(cfg, game, req.get('update', 'all'), req.get('dlc', True))
    Path(cfg['outDir']).mkdir(parents=True, exist_ok=True)
    out = Path(cfg['outDir']) / (safe_name(game['name']) + ('.app' if platform == 'mac-app' else '.ipa'))
    nice = ['nice', '-n', '10'] if cfg.get('lowPriority', '1') != '0' else []
    cmd = nice + [cfg['python'], '-u', 'scripts/build_ipa_from_nsp.py', str(folder), '-k', cfg['keys'],
           '--progress', 'json', '--platform', platform, '-l', lang, '-o', str(out)]
    if platform == 'ios' and req.get('romfsSeparate'):
        cmd += ['--romfs', 'separate']
    if req.get('noMotion'):
        cmd += ['--no-motion']
    if req.get('allowDamaged'):
        cmd += ['--allow-damaged']
    log = folder / 'atelier.log'
    with JOB.cond:
        JOB.events, JOB.result, JOB.cancelled = [], None, False
        JOB.info = {'id': game['id'], 'name': game['name'], 'platform': platform, 'language': lang,
                    'started': time.time(), 'out': str(out), 'log': str(log)}
    head = git(cfg, 'rev-parse', '--short', 'HEAD')[1]
    JOB.push({'ev': 'say', 'msg': f"Fichiers utilisés : " + ', '.join(p['name'] for p in picks)})
    JOB.push({'ev': 'say', 'msg': f"nxnative {head} · {platform} · langue {lang}" + (' · priorité basse' if nice else '')})
    JOB.proc = subprocess.Popen(cmd, cwd=cfg['nxnative'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, env=tool_env(), start_new_session=True)
    threading.Thread(target=pump, args=(JOB.proc, log, cfg, game, platform, lang, str(out), head), daemon=True).start()


def pump(proc, log, cfg, game, platform, lang, out, head):
    lf = open(log, 'a', encoding='utf-8')
    lf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(proc.args)}\n")

    def read_err():
        for line in proc.stderr:
            lf.write('[stderr] ' + line); lf.flush()
            line = line.rstrip()
            if line:
                JOB.push({'ev': 'say', 'msg': line, 'err': True})
    t = threading.Thread(target=read_err, daemon=True); t.start()
    for line in proc.stdout:
        lf.write(line); lf.flush()
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            ev = {'ev': 'say', 'msg': line}
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
                msg = import_save(cfg, game['id'], only_if_empty=True)
                if msg:
                    JOB.push({'ev': 'say', 'msg': msg})
            except Exception as e:
                JOB.push({'ev': 'say', 'msg': f'Après le build : {e}', 'err': True})
    lf.write(f"===== fin, code {code}\n"); lf.close()
    with JOB.cond:
        JOB.result = {'ok': ok, 'code': code, 'out': out, 'cancelled': JOB.cancelled}
    JOB.push({'ev': 'done', 'ok': ok, 'code': code, 'cancelled': JOB.cancelled})
    finish_item(ok, code, JOB.cancelled)


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


def import_save(cfg, gid, only_if_empty):
    """Copie la sauvegarde des anciens portages NX2Apple (ROM/Switch/save/<id>) vers celle des apps nxnative."""
    src = Path(cfg['oldSaveDir']) / gid
    dst = NX_SUPPORT / gid / 'save'
    if not src.is_dir():
        return None
    if dst.is_dir() and any(dst.iterdir()):
        if only_if_empty:
            return None
        backup = dst.with_name(f"save.avant-{time.strftime('%Y%m%d-%H%M%S')}")
        dst.rename(backup)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns('.*'))
    return f"Sauvegarde reprise depuis {src}"


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
        m = re.fullmatch(r'/(icon|art)/([0-9A-F]{16})\.(png|jpg)', u.path)
        if m:
            p = (ICONS if m.group(1) == 'icon' else ARTWORK) / f'{m.group(2)}.{m.group(3)}'
            if p.exists():
                return self.send(200, p.read_bytes(), 'image/png' if m.group(3) == 'png' else 'image/jpeg')
            return self.send(404, {})
        if not self.authed(qs):
            return self.send(403, {'error': 'jeton invalide'})
        cfg = config()
        if u.path == '/api/state':
            games, orphans, archives = scan(cfg)
            catalog = catalog_for(cfg, games)
            nx_ok = (Path(cfg['nxnative']) / 'scripts/build_ipa_from_nsp.py').exists()
            head = git(cfg, 'log', '-1', '--format=%h|%cd', '--date=format:%d/%m %H:%M')[1] if nx_ok else ''
            return self.send(200, {
                'config': cfg, 'games': games, 'orphans': orphans, 'archives': archives, 'unzipping': sorted(UNZIPPING), 'catalog': catalog,
                'rom': bool(cfg['romDir']) and Path(cfg['romDir']).is_dir(), 'keys': Path(cfg['keys']).is_file(),
                'nxnative': {'ok': nx_ok, 'head': head},
                'job': {'busy': JOB.busy(), 'info': JOB.info, 'result': JOB.result, 'item': JOB.item},
                'queue': queue(),
            })
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
                save_json(CONFIG, cur); return self.send(200, {'ok': True})
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
                if not target or not Path(target).exists():
                    return self.send(404, {'error': 'Introuvable.'})
                if req.get('what') in ('log', 'buildlog'):
                    args = ['open', '-a', 'Console', target]
                elif req.get('reveal'):
                    args = ['open', '-R', target]
                else:
                    args = ['open', target]
                subprocess.Popen(args); return self.send(200, {'ok': True})
            if u.path == '/api/import-save':
                msg = import_save(cfg, req['id'], only_if_empty=False)
                return self.send(200, {'ok': True, 'msg': msg or 'Aucune ancienne sauvegarde.'})
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
                return self.send(200, {'ok': True})
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
            if u.path == '/api/nx/pull':
                if JOB.busy():
                    return self.send(409, {'error': 'Attends la fin du build en cours.'})
                code, outp = git(cfg, 'pull', '--ff-only', '--autostash', timeout=600)
                build_catalog(cfg, fetch=False)   # les « nouveau » disparaissent une fois le clone à jour
                return self.send(200, {'ok': code == 0, 'msg': outp})
            return self.send(404, {'error': 'inconnu'})
        except Exception as e:
            return self.send(400, {'error': str(e)})


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
