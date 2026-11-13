#!/usr/bin/env bash
# check_public.sh - fail-closed privacy gate for this repository. Run it before
# any push, and again after every history rewrite.
#
# Usage:
#   tools/check_public.sh [--private-root LABEL=DIR[!PREFIX,...]]... \
#                         [--public-root DIR]... [--no-history] [--allow RULE:PATH:LINE]...
#
#   --private-root  A git checkout whose tracked text must never appear here.
#                   It is compared in 5-line windows. '!PREFIX' excludes subtrees
#                   that are published on purpose (for example '!hackathon/').
#   --public-root   A git checkout that is already public (for example the
#                   MetaViewer AGPL repository). Its windows are subtracted from
#                   the private set, so shared upstream code is not reported.
#   --no-history    Scan only the working tree, not every blob in git history.
#   --allow         Accept one reviewed finding, for example PERSONAL:README.md:44.
#
# The rules:
#   SECRET    Keys, tokens, private keys, and long high-entropy credential values.
#   PRIVATE   Text copied from a private root; source files in private languages.
#   PERSONAL  Emails outside placeholder domains; listed personal terms (kept
#             here only as SHA-256, so this script never publishes them);
#             git author or committer emails.
#   PATH      Home directories, key, cloud-credential and cookie stores, and
#             host-specific runtime directories of the build machine.
#
# Exit codes: 0 clean, 1 findings, 2 usage or environment error.
# Findings print as RULE<TAB>PATH:LINE<TAB>detail. Matched values are masked.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
command -v python3 >/dev/null || { echo "check_public: python3 required" >&2; exit 2; }
command -v git >/dev/null || { echo "check_public: git required" >&2; exit 2; }
exec python3 -I - "$here" "$@" <<'PY'
import hashlib, math, os, re, subprocess, sys

repo = sys.argv[1]
args = sys.argv[2:]
private_roots, public_roots, allows, history = [], [], set(), True
i = 0
while i < len(args):
    a = args[i]
    if a in ('--private-root', '--public-root', '--allow') and i + 1 < len(args):
        v = args[i + 1]; i += 2
        if a == '--private-root':
            if '=' not in v: sys.exit('check_public: --private-root needs LABEL=DIR')
            label, rest = v.split('=', 1)
            d, _, ex = rest.partition('!')
            private_roots.append((label, d, [p for p in ex.split(',') if p]))
        elif a == '--public-root':
            public_roots.append(v)
        else:
            allows.add(v)
        continue
    if a == '--no-history': history = False; i += 1; continue
    print('check_public: unknown argument ' + a, file=sys.stderr); sys.exit(2)

def git(*cmd, cwd=repo, binary=False):
    out = subprocess.run(['git', '-C', cwd, *cmd], capture_output=True, check=True)
    return out.stdout if binary else out.stdout.decode('utf-8', 'replace')

def is_text(data):
    return b'\0' not in data[:8192]

# --- rules (patterns are assembled so this file does not match itself) ----
B = '-----BEGIN '
SECRET_RES = [
    ('private key block', re.compile(re.escape(B) + r'(?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY')),
    ('Google API key', re.compile('AI' + r'za[0-9A-Za-z_\-]{35}')),
    ('AWS access key', re.compile('AK' + r'IA[0-9A-Z]{16}')),
    ('GitHub token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})')),
    ('provider secret key', re.compile(r'\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{24,}')),
    ('Slack token', re.compile(r'\bxox[abprs]-[A-Za-z0-9\-]{10,}')),
    ('Google OAuth token', re.compile(r'\bya' + r'29\.[A-Za-z0-9_\-]{20,}')),
    ('JWT', re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}')),
]
ASSIGN = re.compile(r'(?i)(api[_-]?key|secret|token|passw(?:or)?d|client[_-]?secret|auth)[A-Za-z_]*["\']?\s*[:=]\s*["\']([^"\'\s]{20,})["\']')
PLACEHOLDER = re.compile(r'(?i)(example|placeholder|dummy|fake|test|sample|changeme|redacted|xxxx|\$\{|<|\{\{)')

def entropy(s):
    n = len(s); return -sum(c / n * math.log2(c / n) for c in (s.count(x) for x in set(s)))

EMAIL = re.compile(r'[A-Za-z0-9._%+\-]+@([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,})')
OK_EMAIL_DOMAINS = re.compile(r'(?i)(^|\.)(example\.(com|org|net)|invalid|test|localhost|users\.noreply\.github\.com|gserviceaccount\.com)$')
# sha256(lowercase token) of personal terms: maintainer names, handles, host names.
PERSONAL_SHA = {
    '4993307a750a8e1b8dd0422ffbadb44374f495cd7dcc5872c3a54e1291388968',
    '7ce0f9407761ba96dc7c2858345c2899fd20077dce2c5806dd2669edf78e42e5',
    '1b870d82f148593497625396af86c2be2c4947524960ee781fdc8ed76f2b30eb',
    '8b87b8115fafea33762256a1f3e8ceec87e3a29da7574aa55a6a4a949cf00bc4',
}
TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._\-]*')
H = '/' + 'home/'
PATH_RES = [
    ('home directory', re.compile(re.escape(H) + r'(?!(?:example|user|you|USER|me)/)[A-Za-z0-9._\-]+')),
    ('home directory', re.compile(r'/Users/(?!(?:example|user|you|me)/)[A-Za-z0-9._\-]+/')),
    ('key or cloud credential store', re.compile(r'(?:~|\$HOME|\$\{HOME\}|/)\.(?:ss' + r'h|aws|gnupg|docker/config\.json|kube|netrc|config/gcloud)\b')),
    ('cloud credential file', re.compile(r'application_default_' + r'credentials\.json|credentials\.db|access_tokens\.db')),
    ('browser cookie or login store', re.compile(r'\b(?:Cook' + r'ies(?:-journal)?|Login Data|Local State)\b(?=["\'/ ]|$)')),
    ('host-specific runtime directory', re.compile(r'/tmp/claude-[0-9]+|/dev/shm/vqa-' + r'ramws')),
]
PRIVATE_EXT = {'.go', '.rs', '.vue', '.ts', '.tsx'}

findings = []
def add(rule, where, line, detail):
    key = f'{rule}:{where}:{line}'
    if key in allows: return
    findings.append((rule, f'{where}:{line}', detail))

def mask(s):
    return s[:4] + '…' + str(len(s)) if len(s) > 6 else '…'

def scan_text(where, text):
    for n, line in enumerate(text.splitlines(), 1):
        for label, rx in SECRET_RES:
            m = rx.search(line)
            if m: add('SECRET', where, n, f'{label} {mask(m.group(0))}')
        for m in ASSIGN.finditer(line):
            v = m.group(2)
            if not PLACEHOLDER.search(v) and entropy(v) >= 3.6:
                add('SECRET', where, n, f'high-entropy {m.group(1)} value {mask(v)}')
        for m in EMAIL.finditer(line):
            if not OK_EMAIL_DOMAINS.search(m.group(1)):
                add('PERSONAL', where, n, f'email {mask(m.group(0))}')
        for t in TOKEN.findall(line):
            for part in {t, *re.split(r'[._\-]', t)}:
                if part and hashlib.sha256(part.lower().encode()).hexdigest() in PERSONAL_SHA:
                    add('PERSONAL', where, n, f'listed personal term {mask(part)}'); break
        for label, rx in PATH_RES:
            m = rx.search(line)
            if m: add('PATH', where, n, f'{label} {m.group(0)[:24]}')

# --- 5-line windows for private-source matching ---------------------------
TEXT_EXT = {'.py', '.go', '.rs', '.js', '.mjs', '.ts', '.tsx', '.vue', '.sh', '.md', '.json', '.css', '.html', '.svg', '.yaml', '.yml', '.toml', '.txt', '.rb', '.c', '.h'}
TRIVIAL = re.compile(r'^[\s{}()\[\];,.:]*$|^(?:return|else|end|fi|done|esac|pass|break|continue|\*/|/\*\*?|#|//|"""|\'\'\')\W*$')
W = 5
def norm_lines(text):
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        s = ' '.join(raw.split())
        if len(s) >= 12 and not TRIVIAL.match(s): out.append((n, s))
    return out
def windows(text):
    lines = norm_lines(text)
    for k in range(len(lines) - W + 1):
        chunk = '\n'.join(s for _, s in lines[k:k + W])
        yield hashlib.blake2b(chunk.encode(), digest_size=8).digest(), lines[k][0], lines[k + W - 1][0]
def tracked(root):
    try: names = git('ls-files', '-z', cwd=root).split('\0')
    except Exception: print(f'check_public: cannot list {root}', file=sys.stderr); sys.exit(2)
    for name in names:
        if not name or os.path.splitext(name)[1].lower() not in TEXT_EXT: continue
        p = os.path.join(root, name)
        try:
            if os.path.islink(p) or os.path.getsize(p) > 512 * 1024: continue
            data = open(p, 'rb').read()
        except OSError: continue
        if is_text(data): yield name, data.decode('utf-8', 'replace')

public_set = set()
for root in public_roots:
    for _, text in tracked(root):
        public_set.update(h for h, _, _ in windows(text))
private_index = {}
for label, root, excludes in private_roots:
    for name, text in tracked(root):
        if any(name.startswith(x) for x in excludes): continue
        for h, a, _ in windows(text):
            if h not in public_set: private_index.setdefault(h, (label, name, a))

# --- what gets scanned ------------------------------------------------------
SELF = 'tools/check_public.sh'
work = [f for f in git('ls-files', '-z', '--cached', '--others', '--exclude-standard').split('\0') if f]
for name in sorted(set(work)):
    p = os.path.join(repo, name)
    if not os.path.isfile(p) or os.path.islink(p): continue
    if os.path.splitext(name)[1].lower() in PRIVATE_EXT:
        add('PRIVATE', name, 1, 'source file in a private-tree language')
    data = open(p, 'rb').read()
    if not is_text(data): continue
    text = data.decode('utf-8', 'replace')
    scan_text(name, text)
    if private_index and name != SELF:
        last = -1
        for h, a, b in windows(text):
            hit = private_index.get(h)
            if hit and a > last:
                add('PRIVATE', name, a, f'lines {a}-{b} match {hit[0]} {hit[1]}:{hit[2]}'); last = b

if history and git('rev-parse', '--verify', '-q', 'HEAD').strip():
    seen = set(git('ls-files', '-s').split())  # current blobs are covered above
    for row in git('rev-list', '--all', '--objects').splitlines():
        sha, _, path = row.partition(' ')
        if not path or sha in seen: continue
        if git('cat-file', '-t', sha).strip() != 'blob': continue
        data = git('cat-file', 'blob', sha, binary=True)
        if is_text(data): scan_text(f'history:{path}@{sha[:10]}', data.decode('utf-8', 'replace'))
    for n, who in enumerate(sorted(set(git('log', '--all', '--format=%ae%n%ce').split())), 1):
        dom = who.rpartition('@')[2]
        if who and not OK_EMAIL_DOMAINS.search(dom):
            add('PERSONAL', 'git-metadata', n, f'author/committer email {mask(who)} (rewrite to a noreply address before pushing)')

for rule, where, detail in findings:
    print(f'{rule}\t{where}\t{detail}')
counts = {}
for rule, _, _ in findings: counts[rule] = counts.get(rule, 0) + 1
print(f'check_public: {len(findings)} finding(s) ' + ' '.join(f'{k}={v}' for k, v in sorted(counts.items())) +
      f'; private roots {len(private_roots)}, history {"on" if history else "off"}', file=sys.stderr)
sys.exit(1 if findings else 0)
PY
