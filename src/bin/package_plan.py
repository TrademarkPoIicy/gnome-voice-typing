"""Resolve optional packages without installing them; download/install exactly one artifact."""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import urllib.parse
import urllib.request


def run(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=600, env=dict(os.environ, LC_ALL='C'), **kwargs)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-1800:].strip() or 'Package command failed.')
    return result.stdout


def canonical(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def python_versions(python):
    return json.loads(run([python, '-c', 'import importlib.metadata as m,json; print(json.dumps({d.metadata["Name"]:d.version for d in m.distributions() if d.metadata["Name"]}))']))


def python_inventory(python):
    return {canonical(k): v for k, v in python_versions(python).items()}


def system_version(name, pm):
    if pm == 'dnf': command = ['rpm', '-q', '--qf', '%{VERSION}-%{RELEASE}', name]
    elif pm == 'apt': command = ['dpkg-query', '-W', '-f=${Status}|${Version}', name]
    else: command = ['pacman', '-Q', name]
    try:
        output = run(command).strip()
        if pm == 'apt': return output.split('|', 1)[1] if output.startswith('install ok installed|') else None
        if pm == 'pacman': return output.split(' ', 1)[1]
        return output
    except Exception: return None


def installed(item, python, pm, inventory=None):
    try:
        version = (inventory if inventory is not None else python_inventory(python)).get(canonical(item['package'])) if item['kind'] == 'python' else system_version(item['package'], pm)
        # A later external upgrade is still installed, not an invitation to reinstall our old version.
        return bool(version and (version == item['version'] or version != item.get('previous_version')))
    except Exception: return False


def load(path, python):
    try:
        data = json.loads(Path(path).read_text())
        identity = run([python, '-c', 'import sys; print(sys.version)']).strip()
        if data.get('python') != identity: return None
        return data
    except Exception: return None


def resolve(path, python, pm, system_roots, python_roots, progress):
    items = []
    progress('Checking Python package requirements…')
    with tempfile.TemporaryDirectory(prefix='gvt-package-plan-') as temp:
        report = str(Path(temp) / 'report.json')
        # This is a resolver preview: pip does not modify the environment.
        run([python, '-m', 'pip', 'install', '--dry-run', '--only-binary=:all:', '--disable-pip-version-check', '--report', report] + python_roots)
        for entry in json.loads(Path(report).read_text()).get('install', []):
            metadata, info = entry['metadata'], entry['download_info']
            name = canonical(metadata['name'])
            url = info['url']
            digest = info.get('archive_info', {}).get('hashes', {}).get('sha256')
            if not digest or not urllib.parse.urlsplit(url).path.endswith('.whl'):
                raise RuntimeError('A verified wheel is unavailable for ' + name + '. No packages were installed.')
            items.append({'id': 'python:' + name, 'kind': 'python', 'package': name, 'name': name,
                          'version': metadata['version'], 'url': url, 'sha256': digest,
                          'description': metadata.get('summary') or 'Supporting Python library.'})
    if system_roots:
        progress('Checking system-package requirements…')
        if pm == 'dnf':
            # --url lists the selected RPM and all missing requirements without downloading them.
            output = run(['dnf', 'download', '--url', '--resolve'] + system_roots)
            for line in output.splitlines():
                url = line.strip()
                if not url.startswith(('https://', 'http://')): continue
                filename = urllib.parse.unquote(Path(urllib.parse.urlsplit(url).path).name)
                if not filename.endswith('.rpm'): continue
                stem = filename[:-4].rsplit('.', 1)[0]
                name, version, release = stem.rsplit('-', 2)
                items.append({'id': 'system:' + name, 'kind': 'system', 'package': name, 'name': name,
                              'version': version + '-' + release, 'url': url, 'description': 'System package or supporting library.'})
        elif pm == 'apt':
            output = run(['apt-get', '--print-uris', '--yes', '--download-only', '--no-install-recommends', 'install'] + system_roots)
            for line in output.splitlines():
                if not line.startswith("'"): continue
                parts = shlex.split(line)
                url, filename = parts[:2]
                name, version, arch = urllib.parse.unquote(filename).rsplit('_', 2)
                metadata = run(['apt-cache', 'show', name + '=' + version])
                match = re.search(r'^SHA256: ([a-fA-F0-9]{64})$', metadata, re.M)
                if not match: raise RuntimeError('No repository SHA-256 is available for ' + name)
                items.append({'id': 'system:' + name, 'kind': 'system', 'package': name, 'name': name,
                              'version': version, 'url': url, 'sha256': match[1], 'description': 'System package or supporting library.'})
        elif pm == 'pacman':
            output = run(['pacman', '-Sp', '--print-format', '%n|%v|%l', '--noconfirm'] + system_roots)
            for line in output.splitlines():
                if line.count('|') != 2: continue
                name, version, url = line.split('|')
                items.append({'id': 'system:' + name, 'kind': 'system', 'package': name, 'name': name,
                              'version': version, 'url': url, 'description': 'System package or supporting library.'})
        else: raise RuntimeError('Individual system-package installs are unavailable for this package manager.')
    unique = {item['id']: item for item in items}
    if any('system:' + name not in unique for name in system_roots):
        raise RuntimeError('The package manager did not return all requested package URLs. No packages were installed; refresh the list after checking your repositories.')
    inventory = python_inventory(python)
    for item in unique.values():
        item['previous_version'] = inventory.get(canonical(item['package'])) if item['kind'] == 'python' else system_version(item['package'], pm)
    data = {'python': run([python, '-c', 'import sys; print(sys.version)']).strip(), 'pm': pm,
            'items': sorted(unique.values(), key=lambda item: item['name'])}
    tmp = path + '.tmp'
    Path(tmp).write_text(json.dumps(data, indent=2)); os.replace(tmp, path)
    return data


def download(item, folder, progress):
    url = item['url']
    if urllib.parse.urlsplit(url).scheme not in ('https', 'http'):
        raise RuntimeError('Only repository HTTP(S) package downloads are supported.')
    filename = urllib.parse.unquote(Path(urllib.parse.urlsplit(url).path).name)
    if filename in ('', '.', '..') or '/' in filename or '\\' in filename:
        raise RuntimeError('Invalid package filename.')
    target = Path(folder) / filename
    digest = hashlib.sha256()
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'gnome-voice-typing'}), timeout=60) as response, target.open('wb') as out:
        total = int(response.headers.get('Content-Length') or 0)
        got, previous = 0, -1
        while True:
            chunk = response.read(256 * 1024)
            if not chunk: break
            out.write(chunk); digest.update(chunk); got += len(chunk)
            pct = min(99, int(got * 100 / total)) if total else None
            if pct != previous:
                progress(pct, got, total); previous = pct
        if total and got != total: raise RuntimeError('Download was incomplete. Try this package again.')
    if item.get('sha256') and digest.hexdigest().lower() != item['sha256'].lower():
        raise RuntimeError('Package checksum did not match. Nothing was installed.')
    progress(100, got, total or got)
    return str(target)


def install_one(item, python, pm, progress):
    with tempfile.TemporaryDirectory(prefix='gvt-package-') as temp:
        artifact = download(item, temp, progress)
        lock_path = os.path.join(os.path.dirname(python), '.gvt-' + item['kind'] + '-install.lock')
        with open(lock_path, 'a') as lock:
            progress(100, 0, 0)
            fcntl.flock(lock, fcntl.LOCK_EX)
            if item['kind'] == 'python':
                # A local wheel, no index and no dependency installation: exactly one distribution.
                run([python, '-m', 'pip', 'install', '--no-deps', '--no-index', '--no-build-isolation', '--disable-pip-version-check', artifact])
            elif pm == 'dnf':
                # RPM checks signatures and requirements but never downloads dependency packages.
                signature = run(['rpmkeys', '--checksig', artifact])
                if 'NOKEY' in signature or 'NOT OK' in signature or 'signatures ok' not in signature.lower(): raise RuntimeError('The repository signing key is not trusted. Nothing was installed.')
                run(['pkexec', 'rpm', '-U', '--', artifact])
            elif pm == 'apt':
                # Refuse installation until all supporting packages have been individually installed.
                preview = run(['apt-get', '-s', '--no-install-recommends', 'install', artifact])
                extra = [line.split()[1].split(':')[0] for line in preview.splitlines() if line.startswith('Inst ') and line.split()[1].split(':')[0] != item['package']]
                if extra: raise RuntimeError('Install these listed supporting packages first: ' + ', '.join(extra))
                run(['pkexec', 'dpkg', '--install', artifact])
            elif pm == 'pacman':
                signature_item = dict(item, url=item['url'] + '.sig'); signature_item.pop('sha256', None)
                download(signature_item, temp, lambda *args: None)
                run(['pkexec', 'pacman', '-U', '--noconfirm', artifact])
            else: raise RuntimeError('Unsupported package manager.')
            if not installed(item, python, pm): raise RuntimeError('Installation was not confirmed. This package remains in the list.')
