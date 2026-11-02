"""Private fork QA: five native 9.1.3 observations, not a reliable-watch implementation.

Never run this entry point locally. The existing baseline-labelled Tcl probe is
reused byte-for-byte; the version tested here is explicitly 9.1.3, not unstable.
No build, install, external database, DCO, publication, or performance claim.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import selectors
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.request

REPOSITORY = "xue-ding-e/valkey"
BRANCH = "refs/heads/qa/watch-contract-913-20261102"
BEFORE = "f4a1e222c26e505556b7239156bd8a2aad817c5b"
SOURCE = "29173b67508b2de4c0f0ed551f3b47cf19fc3ca8"
PROBE = "830e9c18f3a9364d837efbe909387f1fac1fc29ed3d8e147930559c66e95211d"
ARCHIVE = "939667f38a99972af6d4ad7045a97876d6db6c4f331f6609290dab2e354fff1e"
URL = "https://download.valkey.io/releases/valkey-9.1.3-jammy-x86_64.tar.gz"
PREFIX = "valkey-9.1.3-jammy-x86_64/bin/"
BINARIES = {
    "valkey-server": (20188008, "29ef0cb8d46d4682e621c38804b1d1e806671895b8a894fefaab3be1570d6db2"),
    "valkey-cli": (8073456, "3eb116800167bd7266ddba815f5636110001c82ccae79660b9898cca900ecbb6"),
}
NEEDED = {"libm.so.6", "libssl.so.3", "libcrypto.so.3", "libc.so.6"}
CASES = (
    "Watch contract: retained unread entries are returned normally",
    "Watch contract: exact trimming does not add an XREAD gap marker",
    "Watch contract: ordinary nonconsecutive IDs are not proof of loss",
    "Watch contract: deleting an unread middle entry is also not signaled",
    "Watch contract: key recreation does not bind XREAD cursor to an epoch",
)
MAX_ARCHIVE = 48 * 1024 * 1024
MAX_LOG = 1024 * 1024


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_event(event, environment):
    require(environment.get("GITHUB_ACTIONS") == "true", "Hosted QA only")
    require(environment.get("RUNNER_ENVIRONMENT") == "github-hosted", "GitHub-hosted runner only")
    require(environment.get("GITHUB_REPOSITORY") == REPOSITORY, "Wrong repository")
    require(environment.get("GITHUB_REF") == BRANCH, "Wrong branch")
    require(environment.get("GITHUB_EVENT_NAME") == "push", "Exact reviewed push only")
    require(event.get("repository", {}).get("full_name") == REPOSITORY, "Wrong event repository")
    require(event.get("ref") == BRANCH and event.get("before") == BEFORE, "Wrong transition")
    after = event.get("after", "")
    require(re.fullmatch(r"[0-9a-f]{40}", after) is not None and after == environment.get("GITHUB_SHA"), "Wrong after SHA")
    require(all(event.get(key) is False for key in ("created", "deleted", "forced")), "Create/delete/force forbidden")


def capture(command, cwd=None, environment=None):
    return subprocess.check_output(command, cwd=cwd, env=environment,
                                   stderr=subprocess.STDOUT, timeout=10).decode()


def fetch_archive(destination):
    started = time.monotonic()
    count = 0
    # No credentials, custom CA, proxy configuration, or insecure transport.
    with urllib.request.urlopen(URL, timeout=15) as response, destination.open("xb") as out:
        require(response.geturl() == URL, "Unexpected artifact redirect")
        while chunk := response.read(256 * 1024):
            count += len(chunk)
            require(count <= MAX_ARCHIVE and time.monotonic() - started < 60, "Download budget exceeded")
            out.write(chunk)
    require(digest(destination.read_bytes()) == ARCHIVE, "Official archive SHA mismatch")
    return count


def extract_binaries(archive_path, destination):
    # No extractall, paths, links, devices or executable scripts from the tarball.
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        require(len(members) <= 20, "Unexpected archive member count")
        for name, (size, expected) in BINARIES.items():
            matches = [member for member in members if member.name == PREFIX + name]
            require(len(matches) == 1, "Missing/duplicate binary")
            member = matches[0]
            require(member.isfile() and member.size == size, "Unexpected binary type/size")
            with archive.extractfile(member) as source:
                content = source.read(size + 1)
            require(len(content) == size and digest(content) == expected, "Binary SHA mismatch")
            target = destination / name
            with target.open("xb") as output:
                output.write(content)
            target.chmod(0o700)


def validate_dynamic_output(name, dynamic, versions, resolved):
    wanted = NEEDED | ({"libsystemd.so.0"} if name == "valkey-server" else set())
    require(set(re.findall(r"Shared library: \[([^]]+)\]", dynamic)) == wanted, "Unexpected shared libraries")
    require("not found" not in resolved, "Missing runner shared library; do not install")
    glibc = [tuple(map(int, match.split("."))) for match in re.findall(r"GLIBC_([0-9.]+)", versions)]
    require(glibc and max(glibc) <= (2, 34), "Unexpected glibc symbol requirement")
    require(set(re.findall(r"OPENSSL_([0-9.]+)", versions)) == {"3.0.0"}, "Unexpected OpenSSL requirement")


def group_members(group):
    result = []
    for row in capture(["ps", "-eo", "pid=,pgid=,sid=,rss="]).splitlines():
        pid, pgid, session, rss = map(int, row.split())
        if pgid == group:
            require(session == group, "Unexpected session ownership")
            result.append((pid, rss))
    return result


def cleanup(proc):
    # This process group/session was created by this Popen; never kill by name.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if group_members(proc.pid):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        if not group_members(proc.pid):
            break
        time.sleep(0.2)
    return group_members(proc.pid)


def observe(source, environment):
    command = ["./runtest", "--single", "unit/watch_resume_contract", "--clients", "1", "--timeout", "30"]
    proc = selector = None
    output = bytearray()
    started = time.monotonic()
    peak_rss = 0
    try:
        # Once Popen succeeds, every later setup failure and cancellation must
        # reach task-owned cleanup, including selector creation/registration.
        proc = subprocess.Popen(command, cwd=source, env=environment, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        while selector.get_map():
            require(time.monotonic() - started < 45, "Harness wall-time budget exceeded")
            rss = sum(row[1] for row in group_members(proc.pid)) * 1024
            peak_rss = max(peak_rss, rss)
            require(rss <= 512 * 1024 * 1024, "Harness RSS budget exceeded")
            for key, _ in selector.select(timeout=0.2):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    output.extend(chunk)
                    require(len(output) <= MAX_LOG, "Harness log budget exceeded")
        proc.wait(timeout=2)
    finally:
        remaining = cleanup(proc) if proc is not None else []
        if selector is not None:
            selector.close()
        if proc is not None and proc.stdout is not None:
            proc.stdout.close()
        text = output[:MAX_LOG].decode(errors="replace").replace(str(source), "$SOURCE")
        print(text, flush=True)
        print(json.dumps({"stage": "native_harness", "seconds": round(time.monotonic() - started, 3),
                          "exit": proc.returncode if proc is not None else None, "peak_rss_bytes": peak_rss,
                          "remaining_task_group_members": remaining}), flush=True)
        require(not remaining, "Task-owned process cleanup incomplete")
    require(proc.returncode == 0, "Harness exit was not success")
    validate_observations(text)


def validate_observations(text):
    require(len(re.findall(r"^\[ok\]: ", text, re.M)) == 5, "Unexpected observation count")
    for name in CASES:
        require(len(re.findall(r"^\[ok\]: " + re.escape(name) + r"(?: \(|$)", text, re.M)) == 1,
                "Missing or duplicate expected observation: " + name)
    require("Test Summary: 5 passed, 0 failed" in text and "[err]" not in text, "Incomplete five-case result")


def main():
    validate_event(json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()), os.environ)
    require(platform.machine() == "x86_64" and platform.system() == "Linux", "Linux x86_64 only")
    workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
    qa, source = workspace / "qa", workspace / "v"
    require(capture(["git", "rev-parse", "HEAD"], qa).strip() == os.environ["GITHUB_SHA"], "Wrong controls SHA")
    require(capture(["git", "show", "-s", "--format=%P", "HEAD"], qa).strip() == BEFORE, "Wrong controls parent")
    require(capture(["git", "rev-parse", "HEAD"], source).strip() == SOURCE, "Wrong official tag source")
    require(not capture(["git", "status", "--porcelain", "--untracked-files=all"], source).strip(), "Source not pristine")
    require('#define VALKEY_VERSION "9.1.3"' in (source / "src/version.h").read_text(), "Wrong harness release")
    require(len(str(source / "tests/tmp/server.9999999.99999/socket").encode()) < 108, "Unix socket fixture path too long")
    for tool in ("tclsh", "readelf", "ldd", "ps"):
        require(shutil.which(tool) is not None, "Missing existing runner tool: " + tool)
    probe = (qa / ".github/probes/watch_resume_contract.tcl").read_bytes()
    require(digest(probe) == PROBE, "Probe changed")
    scope = Path(tempfile.mkdtemp(prefix="vw913-", dir=os.environ["RUNNER_TEMP"]))
    require(shutil.disk_usage(scope).free >= 256 * 1024 * 1024, "Insufficient free space")
    for name in ("bin", "home", "tmp"):
        (scope / name).mkdir(mode=0o700)
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(scope / "home"),
                   "TMPDIR": str(scope / "tmp"), "LC_ALL": "C.UTF-8", "VALKEY_BIN_DIR": str(scope / "bin")}
    archive = scope / "official.tar.gz"
    count = fetch_archive(archive)
    extract_binaries(archive, scope / "bin")
    for name in BINARIES:
        binary = str(scope / "bin" / name)
        dynamic = capture(["readelf", "-d", binary], environment=environment)
        versions = capture(["readelf", "--version-info", binary], environment=environment)
        resolved = capture(["ldd", binary], environment=environment)
        validate_dynamic_output(name, dynamic, versions, resolved)
        version = capture([binary, "--version"], environment=environment).strip()
        require("9.1.3" in version, "Official executable version mismatch")
        print(json.dumps({"binary": name, "sha256": BINARIES[name][1], "version": version,
                          "dynamic_libraries": "resolved"}), flush=True)
    target = source / "tests/unit/watch_resume_contract.tcl"
    with target.open("xb") as output:
        output.write(probe)
    print(json.dumps({"source": SOURCE, "release": "9.1.3", "archive_sha256": ARCHIVE,
                      "archive_bytes": count, "probe_sha256": PROBE, "data": "synthetic only"}), flush=True)
    observe(source, environment)
    require(digest(target.read_bytes()) == PROBE, "Probe changed during observation")
    require(not capture(["git", "diff", "--name-only"], source).strip(), "Tracked source changed")
    require(capture(["git", "status", "--porcelain", "--untracked-files=all"], source).strip() ==
            "?? tests/unit/watch_resume_contract.tcl", "Unexpected source additions")
    print("PASS: five native Valkey 9.1.3 observations only; no reliable-watch, failover, Raft, fairness or performance claim.")


def cancelled(*_):
    raise KeyboardInterrupt("Workflow cancellation")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, cancelled)
    main()
