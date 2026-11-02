"""Offline QA-control tests: no server, socket, network, subprocess or signal."""
import copy
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
import sys
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("watch913", Path(__file__).with_name("watch913.py"))
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)


class ControlTests(unittest.TestCase):
    def event(self):
        return {"repository": {"full_name": qa.REPOSITORY}, "ref": qa.BRANCH,
                "before": qa.BEFORE, "after": "a" * 40,
                "created": False, "deleted": False, "forced": False}

    def environment(self):
        return {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
                "GITHUB_REPOSITORY": qa.REPOSITORY, "GITHUB_REF": qa.BRANCH,
                "GITHUB_EVENT_NAME": "push", "GITHUB_SHA": "a" * 40}

    def test_exact_event_accepted(self):
        qa.validate_event(self.event(), self.environment())

    def test_event_identity_and_transition_fail_closed(self):
        for key, value in [("ref", "refs/heads/unstable"), ("before", "b" * 40),
                           ("after", "b" * 40), ("after", "not-a-sha"),
                           ("created", True), ("deleted", True), ("forced", True),
                           ("forced", None), ("repository", {"full_name": "valkey-io/valkey"})]:
            with self.subTest(key=key, value=value):
                event = self.event()
                event[key] = value
                with self.assertRaises(RuntimeError):
                    qa.validate_event(event, self.environment())

    def test_local_and_wrong_runner_are_refused(self):
        for key, value in [("GITHUB_ACTIONS", "false"), ("RUNNER_ENVIRONMENT", "self-hosted"),
                           ("GITHUB_REPOSITORY", "someone/valkey"), ("GITHUB_REF", "refs/heads/main"),
                           ("GITHUB_EVENT_NAME", "workflow_dispatch"), ("GITHUB_SHA", "b" * 40)]:
            with self.subTest(key=key):
                environment = self.environment()
                environment[key] = value
                with self.assertRaises(RuntimeError):
                    qa.validate_event(self.event(), environment)

    def observation(self):
        return "\n".join("[ok]: " + name + " (1 ms)" for name in qa.CASES) + "\nTest Summary: 5 passed, 0 failed\n"

    def test_exact_five_observations_accepted(self):
        qa.validate_observations(self.observation())

    def test_incomplete_duplicate_failure_extra_or_wrong_summary_refused(self):
        good = self.observation()
        for text in [good.replace(qa.CASES[0], "different"), good + "[ok]: extra (1 ms)\n",
                     good + "[err]: fake\n", good.replace("5 passed", "4 passed"),
                     good.replace(qa.CASES[1], qa.CASES[0]), "PASS"]:
            with self.subTest(text=text[:40]):
                with self.assertRaises(RuntimeError):
                    qa.validate_observations(text)

    def test_dependency_contract(self):
        for name in qa.BINARIES:
            needed = qa.NEEDED | ({"libsystemd.so.0"} if name == "valkey-server" else set())
            dynamic = "\n".join("Shared library: [" + item + "]" for item in sorted(needed))
            qa.validate_dynamic_output(name, dynamic, "GLIBC_2.34 OPENSSL_3.0.0", "all resolved")
            for args in [(dynamic, "GLIBC_2.35 OPENSSL_3.0.0", "all resolved"),
                         (dynamic, "GLIBC_2.34 OPENSSL_3.2.0", "all resolved"),
                         (dynamic, "GLIBC_2.34 OPENSSL_3.0.0", "libssl.so.3 => not found"),
                         (dynamic + "\nShared library: [surprise.so]", "GLIBC_2.34 OPENSSL_3.0.0", "resolved")]:
                with self.subTest(name=name, args=args):
                    with self.assertRaises(RuntimeError):
                        qa.validate_dynamic_output(name, *args)

    def test_probe_unchanged_and_only_five_cases_registered(self):
        probe = Path(__file__).parent.parent / "probes/watch_resume_contract.tcl"
        data = probe.read_bytes()
        self.assertEqual(qa.digest(data), qa.PROBE)
        self.assertEqual(data.count(b"    test {Watch contract:"), 5)
        self.assertNotIn(b"--host", data)

    def archive(self, root, mode):
        path = root / "input.tar.gz"
        payload = b"synthetic binary bytes"
        with tarfile.open(path, "w:gz") as archive:
            member = tarfile.TarInfo(qa.PREFIX + "valkey-server")
            member.size = len(payload)
            if mode == "link":
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
                member.size = 0
            if mode == "traversal":
                member.name = "../../valkey-server"
            archive.addfile(member, io.BytesIO(payload) if member.isfile() else None)
            if mode == "duplicate":
                archive.addfile(copy.copy(member), io.BytesIO(payload))
        return path, payload

    def test_safe_archive_extraction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive, payload = self.archive(root, "regular")
            binaries = {"valkey-server": (len(payload), qa.digest(payload))}
            out = root / "out"
            out.mkdir()
            with patch.object(qa, "BINARIES", binaries):
                qa.extract_binaries(archive, out)
            self.assertEqual((out / "valkey-server").read_bytes(), payload)

    def test_archive_links_traversal_duplicates_and_wrong_hash_refused(self):
        for mode in ("link", "traversal", "duplicate", "hash"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                archive, payload = self.archive(root, mode)
                expected = "0" * 64 if mode == "hash" else qa.digest(payload)
                out = root / "out"
                out.mkdir()
                with patch.object(qa, "BINARIES", {"valkey-server": (len(payload), expected)}):
                    with self.assertRaises(RuntimeError):
                        qa.extract_binaries(archive, out)
                self.assertFalse((out / "valkey-server").exists())

    def test_download_never_accepts_hash_mismatch_or_redirect(self):
        for redirect in (False, True):
            with self.subTest(redirect=redirect), tempfile.TemporaryDirectory() as temp:
                response = io.BytesIO(b"not the official binary")
                response.geturl = lambda: "https://example.invalid/other" if redirect else qa.URL
                with patch.object(qa.urllib.request, "urlopen", return_value=response):
                    with self.assertRaises(RuntimeError):
                        qa.fetch_archive(Path(temp) / "download")

    def test_cleanup_targets_only_the_spawned_group(self):
        process = Mock(pid=123456)
        with patch.object(qa, "group_members", side_effect=[[(123456, 1)], []]), \
                patch.object(qa.os, "killpg") as kill, patch.object(qa.time, "sleep"):
            # One final ownership query is separate from the loop's exit query.
            qa.group_members.side_effect = [[(123456, 1)], [], []]
            self.assertEqual(qa.cleanup(process), [])
            kill.assert_called_once_with(123456, qa.signal.SIGTERM)

    def test_cleanup_escalates_only_owned_group_when_needed(self):
        process = Mock(pid=123456)
        with patch.object(qa, "group_members", side_effect=[[(123456, 1)], [(123456, 1)],
                                                           [(123456, 1)], [], []]), \
                patch.object(qa.os, "killpg") as kill, patch.object(qa.time, "sleep"):
            self.assertEqual(qa.cleanup(process), [])
            self.assertEqual(kill.call_args_list[0].args, (123456, qa.signal.SIGTERM))
            self.assertEqual(kill.call_args_list[1].args, (123456, qa.signal.SIGKILL))

    def test_selector_setup_failure_and_cancellation_always_cleanup(self):
        for stage, exception in (("create", RuntimeError("selector setup")),
                                 ("register", RuntimeError("register setup")),
                                 ("register", KeyboardInterrupt("cancelled"))):
            with self.subTest(stage=stage, exception=type(exception).__name__):
                process = Mock(pid=123456, returncode=-15)
                selector = Mock()
                if stage == "register":
                    selector.register.side_effect = exception
                with patch.object(qa.subprocess, "Popen", return_value=process), \
                        patch.object(qa.selectors, "DefaultSelector", return_value=selector,
                                     side_effect=exception if stage == "create" else None), \
                        patch.object(qa, "cleanup", return_value=[]) as cleanup, patch("builtins.print"):
                    with self.assertRaises(type(exception)):
                        qa.observe(Path("/synthetic/source"), {})
                    cleanup.assert_called_once_with(process)
                    process.stdout.close.assert_called_once_with()
                    if stage == "register":
                        selector.close.assert_called_once_with()

    def test_failed_spawn_never_cleans_an_unowned_process(self):
        with patch.object(qa.subprocess, "Popen", side_effect=OSError("spawn failed")), \
                patch.object(qa, "cleanup") as cleanup, patch("builtins.print"):
            with self.assertRaises(OSError):
                qa.observe(Path("/synthetic/source"), {})
            cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
