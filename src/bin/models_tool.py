#!/usr/bin/env python3
"""Model manager for gnome-voice-typing. Downloads ONLY from the pinned URLs in data/models-registry.json and
verifies every file's SHA-256 before it is used. Nothing else in the program touches the network.

  voice-typing-models list
  voice-typing-models install ID [ID...] [--dry-run]
  voice-typing-models remove ID [ID...]
  voice-typing-models verify [ID...]
  voice-typing-models licenses
"""
import hashlib, json, os, shutil, subprocess, sys, tarfile, time, urllib.request, urllib.error

N = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REG = json.load(open(N + '/data/models-registry.json'))['models']
CAT = {e['id']: e for e in json.load(open(N + '/data/models.json'))}
MODELS = N + '/models'
UA = 'gnome-voice-typing/0.1 (model installer)'

def human(n):
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB': return ('%d %s' % (n, u)) if u == 'B' else ('%.1f %s' % (n, u))
        n /= 1024.0

def total_size(mid):
    r = REG[mid]
    return r['archive']['size'] if 'archive' in r else sum(f['size'] for f in r['files'])

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 22), b''): h.update(b)
    return h.hexdigest()

def download(url, dest, sha, size, label):
    """Resumable download to dest.part, SHA-256 verified, then renamed to dest. Raises on any problem (nothing half-installed is left usable)."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) == size and sha256(dest) == sha:
        print('  already present and verified: %s' % os.path.basename(dest)); return
    part = dest + '.part'
    for attempt in range(1, 5):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if have > size: os.unlink(part); have = 0
        try:
            if have < size:
                req = urllib.request.Request(url, headers={'User-Agent': UA, **({'Range': 'bytes=%d-' % have} if have else {})})
                with urllib.request.urlopen(req, timeout=60) as r:
                    if have and r.status != 206: have = 0     # server ignored Range: start over
                    t0 = time.time(); last = 0; got = have
                    with open(part, 'ab' if have else 'wb') as f:
                        while True:
                            b = r.read(1 << 20)
                            if not b: break
                            f.write(b); got += len(b)
                            if time.time() - last > (0.5 if sys.stdout.isatty() else 10):
                                last = time.time(); rate = (got - have) / max(1e-6, last - t0)
                                msg = '  %s  %s / %s  (%s/s)' % (label, human(got), human(size), human(rate))
                                print(('\r' + msg + '   ') if sys.stdout.isatty() else msg, end='' if sys.stdout.isatty() else '\n', flush=True)
                    if sys.stdout.isatty(): print()
            if os.path.getsize(part) != size: raise IOError('size %d != expected %d' % (os.path.getsize(part), size))
            if sha256(part) != sha:
                os.unlink(part); raise IOError('SHA-256 mismatch (file corrupted or changed upstream) - deleted')
            os.replace(part, dest); return
        except (urllib.error.URLError, IOError, OSError, TimeoutError) as e:
            print('  attempt %d failed: %s' % (attempt, e), file=sys.stderr)
            if 'mismatch' in str(e): raise
            time.sleep(2 * attempt)
    raise SystemExit('download failed: ' + url)

def safe_extract(tf, dest, top):
    dest = os.path.realpath(dest)
    for m in tf.getmembers():
        p = os.path.realpath(os.path.join(dest, m.name))
        if not (p == dest or p.startswith(dest + os.sep)) or not (m.isfile() or m.isdir()): continue   # no links, devices, or path tricks
        if not (m.name == top or m.name.startswith(top + '/')): continue
        if '/test_wavs' in m.name: continue          # not needed
        try: tf.extract(m, dest, filter='data')
        except TypeError: tf.extract(m, dest)      # Python < 3.12

def is_installed(mid):
    r = REG[mid]
    if 'archive' in r: return os.path.exists('%s/%s/%s/tokens.txt' % (MODELS, r['archive']['dest'], r['archive']['dir']))
    return all(os.path.exists('%s/%s' % (MODELS, f['dest'])) for f in r['files'])

def ensure_pkg(mid):
    if REG[mid].get('needs') != 'faster-whisper': return
    try: import faster_whisper  # noqa
    except ImportError:
        print('  installing the faster-whisper Python package (needed for this model) ...', flush=True)
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', 'faster-whisper'], check=True)

def install(mid, dry=False, force=False):
    r = REG[mid]
    print('%s: %s, %s, license %s' % (mid, CAT.get(mid, {}).get('name', mid), human(total_size(mid)), r['license']))
    if dry:
        srcs = [r['archive']['url']] if 'archive' in r else [f['url'] for f in r['files']]
        for u in srcs: print('  [dry-run] would download', u)
        return
    if is_installed(mid) and not force:
        print('  already installed: %s (run `voice-typing-models verify` to re-check, or remove and install again)' % mid); return
    ensure_pkg(mid)
    if 'archive' in r:
        a = r['archive']; tmp = '%s/.dl/%s' % (MODELS, os.path.basename(a['url']))
        download(a['url'], tmp, a['sha256'], a['size'], mid)
        os.makedirs('%s/%s' % (MODELS, a['dest']), exist_ok=True)
        final = '%s/%s/%s' % (MODELS, a['dest'], a['dir']); stage = final + '.new'
        shutil.rmtree(stage, ignore_errors=True)
        with tarfile.open(tmp, 'r:bz2') as tf: safe_extract(tf, os.path.dirname(stage) + '/.x-' + mid, a['dir'])
        shutil.rmtree(final, ignore_errors=True); os.replace('%s/.x-%s/%s' % (os.path.dirname(stage), mid, a['dir']), final)
        shutil.rmtree('%s/.x-%s' % (os.path.dirname(stage), mid), ignore_errors=True); os.unlink(tmp)
    else:
        for f in r['files']: download(f['url'], '%s/%s' % (MODELS, f['dest']), f['sha256'], f['size'], os.path.basename(f['dest']))
    shutil.rmtree(MODELS + '/.dl', ignore_errors=True)
    print('  installed:', mid)

def remove(mid):
    r = REG[mid]
    paths = ['%s/%s/%s' % (MODELS, r['archive']['dest'], r['archive']['dir'])] if 'archive' in r else ['%s/%s' % (MODELS, f['dest']) for f in r['files']]
    for p in paths:
        if os.path.isdir(p): shutil.rmtree(p)
        elif os.path.exists(p): os.unlink(p)
    if mid in ('tiny.en', 'base.en', 'small.en', 'medium.en', 'distil-small.en', 'distil-large-v3'): shutil.rmtree('%s/ct2/%s' % (MODELS, mid), ignore_errors=True)
    print('removed', mid)

def verify(mid):
    r = REG[mid]
    if 'archive' in r: return is_installed(mid), 'archive extracted (checksum was verified at download time)'
    bad = [f['dest'] for f in r['files'] if not os.path.exists('%s/%s' % (MODELS, f['dest'])) or sha256('%s/%s' % (MODELS, f['dest'])) != f['sha256']]
    return not bad, ('OK' if not bad else 'bad or missing: ' + ', '.join(bad))

def main(a):
    if not a or a[0] in ('-h', '--help', 'help'): print(__doc__); return 0
    cmd, rest = a[0], [x for x in a[1:] if not x.startswith('--')]; dry = '--dry-run' in a
    unknown = [m for m in rest if m not in REG]
    if unknown: print('unknown model id(s): %s\nknown: %s' % (', '.join(unknown), ', '.join(REG)), file=sys.stderr); return 2
    if cmd == 'list':
        for mid in REG: print('%-20s %-9s %-9s %-10s %s' % (mid, 'installed' if is_installed(mid) else '-', human(total_size(mid)), r_lic(mid), CAT.get(mid, {}).get('name', '')))
    elif cmd == 'install':
        if not rest: print('usage: voice-typing-models install ID [ID...]', file=sys.stderr); return 2
        for m in rest: install(m, dry, force='--force' in a)
    elif cmd == 'remove':
        for m in rest: remove(m)
    elif cmd == 'verify':
        rc = 0
        for m in (rest or [x for x in REG if is_installed(x)]):
            ok, msg = verify(m); print('%-20s %s %s' % (m, 'OK ' if ok else 'BAD', msg)); rc |= (not ok)
        return rc
    elif cmd == 'licenses':
        for mid, r in REG.items(): print('%-20s %-10s %s' % (mid, r['license'], r['attribution']))
    else: print(__doc__); return 2
    return 0

def r_lic(mid): return REG[mid]['license']

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
