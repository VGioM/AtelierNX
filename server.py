#!/usr/bin/env python3
"""Atelier NX : serveur local qui pilote nxnative (seedds/nxnative) pour l'interface HTML.

Écoute sur 127.0.0.1 uniquement ; chaque requête /api doit porter le jeton injecté dans la page.
Lancé par Atelier NX.app (ou à la main : python3 server.py --open).
"""
import json, os, re, secrets, shutil, signal, struct, subprocess, sys, threading, time, argparse, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
SUPPORT = Path.home() / 'Library/Application Support/AtelierNX'
CONFIG = SUPPORT / 'config.json'
STATE = SUPPORT / 'state.json'
ICONS = SUPPORT / 'icons'
ARTWORK = Path.home() / 'Library/Application Support/NS Port Builder/artwork'
NX_SUPPORT = Path.home() / 'Library/Application Support/nxnative'

DEFAULTS = {
    'nxnative': str(Path.home() / 'Downloads/Archive/nxnative'),
    'python': str(Path.home() / 'Downloads/Archive/nxnative-venv/bin/python'),
    'romDir': '/Volumes/Lexar/Jeux/ROM/Switch',
    'keys': str(Path.home() / 'Library/Application Support/system/prod.keys'),
    'outDir': '/Volumes/Lexar/Jeux/ROM/Switch/Apps',
    'workDir': '/Volumes/Lexar/Jeux/ROM/Switch/nxnative-build',
    'oldSaveDir': '/Volumes/Lexar/Jeux/ROM/Switch/save',
    'language': 'fr',
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


def config():
    return {**DEFAULTS, **load_json(CONFIG, {})}


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

SKIP_DIRS = {'nxnative-build', 'Apps', 'save', 'log', 'Mods', 'imgs'}
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
        m = re.match(r'#\s+(.+?)\s+\(`', first)
        return m.group(1) if m else None
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
    return re.sub(r'[:/]', ' - ', n).strip() or 'jeu'


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
    cmd = [cfg['python'], '-u', 'scripts/build_ipa_from_nsp.py', str(folder), '-k', cfg['keys'],
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
    JOB.push({'ev': 'say', 'msg': f"nxnative {head} · {platform} · langue {lang}"})
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
            make_icon(game['id'], out)
            msg = import_save(cfg, game['id'], only_if_empty=True)
            if msg:
                JOB.push({'ev': 'say', 'msg': msg})
    lf.write(f"===== fin, code {code}\n"); lf.close()
    with JOB.cond:
        JOB.result = {'ok': ok, 'code': code, 'out': out, 'cancelled': JOB.cancelled}
    JOB.push({'ev': 'done', 'ok': ok, 'code': code, 'cancelled': JOB.cancelled})


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
            nx_ok = (Path(cfg['nxnative']) / 'scripts/build_ipa_from_nsp.py').exists()
            head = git(cfg, 'log', '-1', '--format=%h|%cd', '--date=format:%d/%m %H:%M')[1] if nx_ok else ''
            return self.send(200, {
                'config': cfg, 'games': games, 'orphans': orphans, 'archives': archives, 'unzipping': sorted(UNZIPPING),
                'rom': Path(cfg['romDir']).is_dir(), 'keys': Path(cfg['keys']).is_file(),
                'nxnative': {'ok': nx_ok, 'head': head},
                'job': {'busy': JOB.busy(), 'info': JOB.info, 'result': JOB.result},
            })
        if u.path == '/api/busy':
            return self.send(200, {'busy': JOB.busy()})
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
                if JOB.busy():
                    return self.send(409, {'error': 'Un build est déjà en cours.'})
                start_build(cfg, req)
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
            if u.path == '/api/nx/pull':
                if JOB.busy():
                    return self.send(409, {'error': 'Attends la fin du build en cours.'})
                code, outp = git(cfg, 'pull', '--ff-only', '--autostash', timeout=600)
                return self.send(200, {'ok': code == 0, 'msg': outp})
            return self.send(404, {'error': 'inconnu'})
        except Exception as e:
            return self.send(400, {'error': str(e)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=0)
    ap.add_argument('--open', action='store_true', help='ouvrir la page dans le navigateur')
    a = ap.parse_args()
    srv = ThreadingHTTPServer(('127.0.0.1', a.port), Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]

    def stop(*_):
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
    print(f'PORT {port}', flush=True)
    if a.open:
        webbrowser.open(f'http://127.0.0.1:{port}/')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        stop()


if __name__ == '__main__':
    main()
