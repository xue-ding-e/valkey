"""Isolated QA only: no publication, installation, DCO, or product modifications."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time

BASE = 'f2ca837edc75e2238f880d89ade60babb6e6ed9e'
PATCH = '317edde1703667106e392f534ff52edfa336a9a9074e725444d12f09b70ec39f'
BEFORE = '3d8ed0403bc042fbf7e499d54c578dc157b48a3d6ee3ee4a9a9faa865ef46f13'
AFTER = '4df8326aee567571dec05914947af3e2803477a16f3b36f7229af274e272edf9'
MIB = 1024 * 1024


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def members(pgid):
    rows = subprocess.check_output(['ps', '-eo', 'pid=,pgid=,rss=']).decode().splitlines()
    return [tuple(map(int, row.split())) for row in rows if int(row.split()[1]) == pgid]


def stop_group(proc):
    # Only our own group, inspected from this same PID namespace.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if members(proc.pid):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.2)
    return members(proc.pid)


def run_stage(scope, source, environment, name, command):
    log = scope / (name + '.log')
    reasons = []
    count = [0]
    peak_rss = peak_disk = 0
    started = time.monotonic()
    initial_disk = int(subprocess.check_output(['du', '-sk', str(scope)]).split()[0]) * 1024
    if initial_disk > 800 * MIB or shutil.disk_usage(scope).free < 192 * MIB:
        raise RuntimeError('Pre-launch disk budget failed; no process started')
    proc = subprocess.Popen(command, cwd=source, env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)

    def drain():
        with log.open('wb') as stream:
            while chunk := proc.stdout.read1(8192):
                previous_logs = sum(f.stat().st_size for f in scope.glob('*.log') if f != log)
                available = max(0, 16 * MIB - previous_logs - count[0])
                stream.write(chunk[:available])
                stream.flush()
                count[0] += len(chunk)
                if previous_logs + count[0] > 16 * MIB and 'log limit' not in reasons:
                    reasons.append('log limit')

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        while proc.poll() is None:
            rss = sum(row[2] for row in members(proc.pid)) * 1024
            disk = int(subprocess.check_output(['du', '-sk', str(scope)]).split()[0]) * 1024
            peak_rss, peak_disk = max(peak_rss, rss), max(peak_disk, disk)
            if rss > 3 * 1024 * MIB:
                reasons.append('group RSS above 3 GiB')
            if disk > 800 * MIB:
                reasons.append('scope above 800 MiB')
            if shutil.disk_usage(scope).free < 192 * MIB:
                reasons.append('free space below 192 MiB')
            if (scope / 'STOP').exists():
                reasons.append('operator stop file')
            if reasons:
                break
            time.sleep(1)
    finally:
        remaining = stop_group(proc)
        reader.join(timeout=5)
        if reader.is_alive():
            reasons.append('log reader did not finish')
        result = dict(stage=name, command=command, exit=proc.returncode,
                      seconds=round(time.monotonic() - started, 1), reasons=reasons,
                      peak_rss_bytes=peak_rss, peak_scope_bytes=peak_disk,
                      log_bytes=count[0], remaining_group_members=remaining)
        (scope / (name + '.json')).write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
        # At most 16 MiB was retained; print actual case evidence to the job log.
        print(log.read_text(errors='replace'), flush=True)
    if proc.returncode or reasons or remaining:
        raise RuntimeError('Stage failed or guard stopped it: ' + name)


def main():
    if os.environ.get('GITHUB_REPOSITORY') != 'xue-ding-e/valkey':
        raise RuntimeError('Wrong repository; this script is hosted QA only')
    if os.environ.get('GITHUB_REF') != 'refs/heads/qa/loading-regression-20261028':
        raise RuntimeError('Wrong validation branch')
    if os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch':
        raise RuntimeError('Manual validation only')
    scope = Path(os.environ['GITHUB_WORKSPACE']).resolve()
    source = scope / 'v'
    patch = scope / 'qa/.github/patches/debug-loading-regression.patch'
    test = source / 'tests/integration/replication.tcl'
    for tool in ('cc', 'make', 'tclsh', 'timeout', 'git', 'ps', 'du'):
        if not shutil.which(tool):
            raise RuntimeError('Missing existing runner tool: ' + tool)
    if subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source).decode().strip() != BASE:
        raise RuntimeError('Wrong official source SHA')
    subprocess.run(['git', 'diff', '--exit-code'], cwd=source, check=True)
    if sha(patch) != PATCH or sha(test) != BEFORE:
        raise RuntimeError('Wrong patch or preimage')
    subprocess.run(['git', 'apply', '--check', str(patch)], cwd=source, check=True)
    subprocess.run(['git', 'apply', str(patch)], cwd=source, check=True)
    if sha(test) != AFTER:
        raise RuntimeError('Unexpected test postimage')
    changed = subprocess.check_output(['git', 'diff', '--name-only'], cwd=source).decode().splitlines()
    if changed != ['tests/integration/replication.tcl']:
        raise RuntimeError('Unexpected source changes')
    socket_path = str(source / 'tests/tmp/server.9999999.99999/socket')
    if len(socket_path.encode()) >= 108:
        raise RuntimeError('Runner checkout path too long for Unix socket fixture')
    temp = scope / 'compiler-tmp'
    temp.mkdir(mode=0o700)
    environment = os.environ.copy()
    environment['TMPDIR'] = str(temp)
    for name, command in [
        ('build', ['timeout', '-k', '5', '900', 'make', '-j1', 'MALLOC=libc', 'OPTIMIZATION=-O1', 'DEBUG=']),
        ('module', ['timeout', '-k', '5', '120', 'make', '-C', 'tests/modules', 'basics.so']),
        ('replication', ['timeout', '-k', '5', '660', './runtest', '--single', 'integration/replication', '--clients', '1', '--timeout', '600']),
    ]:
        run_stage(scope, source, environment, name, command)
    output = (scope / 'replication.log').read_text(errors='replace')
    for expected in ('[ok]: DEBUG reload commands are blocked during loading',
                     '[ok]: Blocked commands and configs during async-loading'):
        if expected not in output:
            raise RuntimeError('Missing real regression case evidence: ' + expected)
    print('CURRENT-BASE GREEN ONLY: not old-product RED, upstream contribution, or performance evidence.')


def cancelled(*_):
    raise KeyboardInterrupt('Workflow cancellation')


if __name__ == '__main__':
    # Cancellation must also reach the finally cleanup path.
    signal.signal(signal.SIGTERM, cancelled)
    main()
