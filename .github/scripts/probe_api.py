"""Compile-only real Kbuild signature probes. No missing-header fallback."""
import os, pathlib, subprocess, sys, tempfile
header=pathlib.Path(sys.argv[1]).resolve(strict=True)
expected=sys.argv[2]
assert (header/'include/config/kernel.release').read_text().strip()==expected
cc=os.environ.get('CC','gcc-13')
base=pathlib.Path(tempfile.mkdtemp(prefix='rxe-api-probe-',dir=os.environ['RUNNER_TEMP']))
prototypes={
 'setup_socket':'void (*)(struct net *, struct socket *, struct udp_tunnel_sock_cfg *)',
 'setup_sock':'void (*)(struct net *, struct sock *, struct udp_tunnel_sock_cfg *)',
 'release_socket':'void (*)(struct socket *)',
 'release_sock':'void (*)(struct sock *)',
}
def compile_probe(name, assertion=''):
    work=base/name; work.mkdir()
    (work/'Makefile').write_text('obj-m := probe.o\n')
    (work/'probe.c').write_text('#include <linux/module.h>\n#include <net/udp_tunnel.h>\n'+assertion+'\n')
    r=subprocess.run(['timeout','30s','make','-s','-C',str(header),f'M={work}',f'CC={cc}','probe.o'],capture_output=True,text=True)
    (work/'compile.log').write_text(r.stdout+r.stderr)
    print(name,r.returncode,r.stdout,r.stderr,flush=True)
    if r.returncode in (124,137): raise RuntimeError('probe timed out')
    if r.returncode != 0 and name != 'include_control':
        assert 'static assertion failed' in r.stderr and f'RXE_SIGNATURE_MISMATCH_{name}' in r.stderr, 'Unexpected probe failure'
    return r.returncode
assert compile_probe('include_control')==0,'Header/toolchain control failed; no classification'
results={}
for name,proto in prototypes.items():
    function='setup_udp_tunnel_sock' if name.startswith('setup') else 'udp_tunnel_sock_release'
    assertion=f'static_assert(__builtin_types_compatible_p(typeof(&{function}), {proto}), "RXE_SIGNATURE_MISMATCH_{name}");'
    results[name]=compile_probe(name,assertion)==0
for family in ['setup','release']:
    assert sum(results[k] for k in results if k.startswith(family+'_'))==1, 'Unknown/ambiguous signature'
print(expected,results,flush=True)
