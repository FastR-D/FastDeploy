#!/usr/bin/env python3
"""Provision a restricted research-fetch SSH account and repository Secrets."""
import argparse
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = 'fastnews-fetch'
STATE = '/home/dev/.config/fastnews-fetch'


def run(args, data=None):
    return subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=90).stdout


def remote(alias, code):
    return run(['ssh', '-o', 'BatchMode=yes', alias, 'python3 -'], code.encode())


def pinned_host(alias):
    settings = dict(line.split(' ', 1) for line in run(['ssh', '-G', alias]).decode().splitlines() if ' ' in line)
    host, port = settings['hostname'], settings['port']
    lookup = host if port == '22' else f'[{host}]:{port}'
    entries = []
    for filename in shlex.split(settings['userknownhostsfile']):
        path = Path(os.path.expanduser(filename.replace('%d', str(Path.home()))))
        if path.is_file():
            result = subprocess.run(['ssh-keygen', '-F', lookup, '-f', str(path)], capture_output=True, text=True)
            entries.extend(line for line in result.stdout.splitlines() if line and not line.startswith('#'))
    if not entries:
        raise ValueError('trusted known_hosts entry is required')
    return host, port, '\n'.join(entries) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy-alias', default='fastrd-proxy')
    parser.add_argument('--app-alias', default='fastrd')
    parser.add_argument('--allowlist', required=True, type=Path)
    parser.add_argument('--repository', default='FastR-D/FastNews')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    allowed = json.loads(args.allowlist.read_text())
    if not isinstance(allowed, list) or not allowed or any(not isinstance(u, str) or not u.startswith('https://') for u in allowed):
        raise ValueError('expected HTTPS URL allowlist')
    host, port, known = pinned_host(args.proxy_alias)
    if not args.apply:
        print(f'Plan: restricted {ACCOUNT} account, {len(allowed)} endpoints, five production repository Secrets')
        return
    # Check GitHub access before creating any credentials.
    run(['gh', 'secret', 'list', '--repo', args.repository, '--env', 'production', '--json', 'name'])
    existing = remote(args.app_alias, f'from pathlib import Path\np=Path({STATE!r})/"id_ed25519"\nimport sys\nsys.stdout.buffer.write(p.read_bytes() if p.exists() else b"")\n')
    with tempfile.TemporaryDirectory(prefix='fastnews-relay-') as temp:
        key = Path(temp) / 'id_ed25519'
        if existing:
            key.write_bytes(existing)
            key.chmod(0o600)
        else:
            run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'fastnews-fetch-relay', '-f', str(key)])
        private = key.read_bytes()
        public = run(['ssh-keygen', '-y', '-f', str(key)]).decode().strip()
        payload = base64.b64encode(json.dumps({'public': public, 'allowed': allowed, 'script': (ROOT / 'scripts/fetch_relay.py').read_text()}).encode()).decode()
        remote(args.proxy_alias, """
import base64,json,os,pwd,subprocess
from pathlib import Path
assert os.geteuid()==0, 'provisioning requires proxy administrator'
data=json.loads(base64.b64decode(PAYLOAD))
name='fastnews-fetch'
try:
 user=pwd.getpwnam(name)
 assert user.pw_dir=='/var/lib/fastnews-fetch' and Path('/etc/fastnews-relay/managed').exists(), 'unmanaged account'
except KeyError:
 subprocess.run(['useradd','--system','--create-home','--home-dir','/var/lib/fastnews-fetch','--shell','/bin/sh',name],check=True)
root=Path('/etc/fastnews-relay');root.mkdir(mode=0o755,exist_ok=True)
(root/'managed').touch()
for name,body in [('fetch.py',data['script']),('allowlist.json',json.dumps(data['allowed']))]:
 tmp=root/(name+'.tmp');tmp.write_text(body);tmp.chmod(0o644);tmp.replace(root/name)
home=Path('/var/lib/fastnews-fetch');os.chown(home,0,0);home.chmod(0o755)
ssh=home/'.ssh';ssh.mkdir(exist_ok=True);os.chown(ssh,0,0);ssh.chmod(0o755)
auth=ssh/'authorized_keys'
auth.write_text('restrict,command="/usr/bin/python3 /etc/fastnews-relay/fetch.py" '+data['public']+'\\n')
os.chown(auth,0,0);auth.chmod(0o644)
""".replace('PAYLOAD', repr(payload)))
        config = f'''Host fastnews-fetch
  HostName {host}
  Port {port}
  User {ACCOUNT}
  IdentityFile {STATE}/id_ed25519
  UserKnownHostsFile {STATE}/known_hosts
  StrictHostKeyChecking yes
  IdentitiesOnly yes
  BatchMode yes
  RequestTTY no
  ForwardAgent no
  ClearAllForwardings yes
  ConnectTimeout 12
  ServerAliveInterval 15
  ServerAliveCountMax 3
  LogLevel ERROR
'''
        data = base64.b64encode(json.dumps({'private': private.decode(), 'known': known, 'config': config}).encode()).decode()
        remote(args.app_alias, f'''
import base64,json
from pathlib import Path
x=json.loads(base64.b64decode({data!r}));root=Path({STATE!r});root.mkdir(mode=0o700,parents=True,exist_ok=True);root.chmod(0o700)
for name,content in [('id_ed25519',x['private']),('known_hosts',x['known']),('ssh_config',x['config'])]:
 p=root/(name+'.tmp');p.write_text(content);p.chmod(0o600);p.replace(root/name)
''')
        # Verify the fixed command and pinned host from the application machine.
        probe = json.dumps({'url': allowed[0]})
        remote(args.app_alias, f'''
import subprocess,json
r=subprocess.run(['ssh','-F',{STATE + '/ssh_config'!r},'fastnews-fetch'],input={probe!r}.encode(),capture_output=True,timeout=95)
assert r.returncode==0 and len(r.stdout)>0, 'relay verification failed'
''')
        values = {'FASTNEWS_RELAY_SSH_HOST': host, 'FASTNEWS_RELAY_SSH_PORT': port,
                  'FASTNEWS_RELAY_SSH_USER': ACCOUNT, 'FASTNEWS_RELAY_SSH_KNOWN_HOSTS': known,
                  'FASTNEWS_RELAY_SSH_PRIVATE_KEY': private.decode()}
        for name, value in values.items():
            run(['gh', 'secret', 'set', name, '--repo', args.repository, '--env', 'production'], value.encode())
            print(f'Configured {name}', flush=True)
        print('Verified restricted relay; local temporary key removed at exit')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Relay provisioning failed; inspect account ownership, pinned host and GitHub permissions (credentials suppressed)')
