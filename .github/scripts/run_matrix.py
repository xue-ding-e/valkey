"""Serial compile-only RXE matrix. Never install or load modules."""
import hashlib, os, pathlib, shutil, subprocess
root=pathlib.Path(os.environ['RUNNER_TEMP'])/'rxe-compile-matrix'
root.mkdir(exist_ok=False)
source=pathlib.Path('rxe').resolve()
scripts=pathlib.Path('.github/scripts').resolve()
patch=scripts/'rxe-api-candidate.patch'
base_hash='71783310ffa375ed24482816003bcb90bf10e27d5c28dd92a22af6f57df297ec'
candidate_hash='4ed66bd00d2f390cd5431e529859c5b7fb9a2b0529555ff8bc27d7e2b2ce54c3'
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
assert sha(patch)=='4c71f6911bd8be4a98a770187811efccc7676b903bce83c045c6fbc0d6a280a5'
assert sha(source/'rxe_net.c')==base_hash
assert subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()=='cdcd539ade4e2797bcc4da9161a26feca997a9a4'
old=pathlib.Path('/usr/src/linux-headers-6.17.0-1022-azure')
new=pathlib.Path(os.environ['RXE_HEADERS'])
if not (old/'include/config/kernel.release').is_file():
    raise RuntimeError('BLOCKED_OLD_HEADER: exact 6.17.0-1022-azure headers unavailable; no old-API claim')
assert (old/'include/config/kernel.release').read_text().strip()=='6.17.0-1022-azure'
assert (new/'include/config/kernel.release').read_text().strip()=='7.0.0-1017-azure'
subprocess.run(['dpkg-query','-W','linux-headers-6.17.0-1022-azure'],check=True)
print('BASELINE_SHA256',base_hash,'CANDIDATE_SHA256',candidate_hash,flush=True)
# Negative control: a nonexistent header tree must fail before classification.
negative=subprocess.run(['python3',str(scripts/'probe_api.py'),str(root/'missing-headers'),'missing'],capture_output=True,text=True)
(root/'negative.log').write_text(negative.stdout+negative.stderr)
print('NEGATIVE',negative.returncode,negative.stdout,negative.stderr,flush=True)
assert negative.returncode != 0 and 'FileNotFoundError' in negative.stderr and 'setup_socket' not in negative.stdout
for label,header,release in [('old',old,'6.17.0-1022-azure'),('target',new,'7.0.0-1017-azure')]:
    subprocess.run(['python3',str(scripts/'probe_api.py'),str(header),release],check=True,timeout=180)
    for variant in ['baseline','candidate']:
        target=root/f'{label}-{variant}'
        shutil.copytree(source,target,ignore=shutil.ignore_patterns('.git'))
        if variant=='candidate':
            subprocess.run(['git','apply','--check',str(patch)],cwd=target,check=True)
            subprocess.run(['git','apply',str(patch)],cwd=target,check=True)
            assert sha(target/'rxe_net.c')==candidate_hash
        else: assert sha(target/'rxe_net.c')==base_hash
        result=subprocess.run(['timeout','300s','make','-j2','-C',str(header),f'M={target}','CC=gcc-13','modules'],capture_output=True,text=True)
        output=result.stdout+result.stderr
        (root/f'{label}-{variant}.log').write_text(output)
        print('BUILD',label,variant,'EXIT',result.returncode,output,flush=True)
        if label=='target' and variant=='baseline':
            assert result.returncode not in (0,124,137)
            assert all(t in output for t in ['rxe_net.c:200','setup_udp_tunnel_sock','incompatible pointer type','struct socket *','struct sock *'])
        else:
            assert result.returncode==0
            assert (target/'rdma_rxe.ko').is_file()
print('COMPILE_MATRIX_COMPLETE: old baseline/candidate and target candidate compiled; target baseline failed as expected. NO MODULE LOAD, RDMA RUNTIME OR PERFORMANCE TEST.',flush=True)
