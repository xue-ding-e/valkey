"""Download and extract authenticated Ubuntu headers; never install packages."""
import hashlib, lzma, os, pathlib, re, subprocess, urllib.request
BASE = 'https://archive.ubuntu.com/ubuntu/'
VERSION = '6.17.0-1022.22'
PACKAGES = {
 'linux-azure-6.17-headers-6.17.0-1022': ('all',14442750,'76af6797e536169fde8782545bb043cbbb011708c7fc6fec51799c6aff22c3a7'),
 'linux-headers-6.17.0-1022-azure': ('amd64',3515486,'7e800c3af8998d33df25cad0b26c789f5e0b583fb1f84090f83e1fd5099e4d5a'),
}
work = pathlib.Path(os.environ['RUNNER_TEMP'])/'rxe-authenticated-old-headers'
work.mkdir(exist_ok=False)
def download(relative, dest, limit):
    with urllib.request.urlopen(BASE+relative, timeout=45) as r, open(dest,'wb') as out:
        total=0
        while chunk := r.read(65536):
            total += len(chunk)
            if total > limit: raise RuntimeError('download exceeds size cap')
            out.write(chunk)
    return dest.read_bytes()
def digest(data): return hashlib.sha256(data).hexdigest()
release=work/'InRelease'
download('dists/noble-updates/InRelease',release,250000)
subprocess.run(['gpgv','--keyring','/usr/share/keyrings/ubuntu-archive-keyring.gpg',str(release)],check=True,timeout=15)
signed=release.read_text()
assert '\nOrigin: Ubuntu\n' in signed and '\nSuite: noble-updates\n' in signed
section=signed.split('\nSHA256:\n',1)[1].split('\nSHA512:',1)[0]
rows=[x.split() for x in section.splitlines()]
entry=next(x for x in rows if len(x)==3 and x[2]=='main/binary-amd64/Packages.xz')
index_size=int(entry[1]); assert index_size <= 10000000
index=download('dists/noble-updates/'+entry[2],work/'Packages.xz',index_size)
assert len(index)==index_size and digest(index)==entry[0]
with lzma.open(work/'Packages.xz','rt') as f: text=f.read(80000001)
assert len(text)<=80000000
records={}
for paragraph in text.split('\n\n'):
    fields={}
    for line in paragraph.splitlines():
        if line and not line[0].isspace() and ': ' in line:
            k,v=line.split(': ',1); fields[k]=v
    if fields.get('Package') in PACKAGES and fields.get('Version')==VERSION:
        records[fields['Package']]=fields
root=work/'root'; root.mkdir()
for name,(arch,size,sha) in PACKAGES.items():
    record=records[name]
    expected=f'pool/main/l/linux-azure-6.17/{name}_{VERSION}_{arch}.deb'
    assert record['Filename']==expected and record['Architecture']==arch
    assert int(record['Size'])==size and record['SHA256']==sha
    deb=work/(name+'.deb'); payload=download(expected,deb,size)
    assert len(payload)==size and digest(payload)==sha
    for field,want in [('Package',name),('Version',VERSION),('Architecture',arch)]:
        got=subprocess.check_output(['dpkg-deb','-f',str(deb),field],text=True).strip()
        assert got==want,(field,got,want)
    print(name,VERSION,arch,size,sha,flush=True)
    subprocess.run(['dpkg-deb','-x',str(deb),str(root)],check=True,timeout=30)
header=root/'usr/src/linux-headers-6.17.0-1022-azure'
assert (header/'include/config/kernel.release').read_text().strip()=='6.17.0-1022-azure'
for relative in ['Makefile','include/net/udp_tunnel.h','include/generated/autoconf.h','scripts/Makefile.build','tools/objtool/objtool','scripts/mod/modpost']:
    p=(header/relative).resolve(strict=True)
    assert p.is_relative_to(root.resolve()),(relative,str(p))
    print('verified-path',relative,str(p),flush=True)
print((header/'include/generated/compile.h').read_text(),flush=True)
for tool in ['tools/objtool/objtool','scripts/mod/modpost']:
    result=subprocess.run(['ldd',str(header/tool)],capture_output=True,text=True,timeout=10)
    print(result.stdout,result.stderr,flush=True)
    assert result.returncode==0 and 'not found' not in result.stdout
with open(os.environ['GITHUB_ENV'],'a') as f: f.write(f'RXE_OLD_HEADERS={header}\n')
