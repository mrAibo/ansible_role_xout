import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "xmanager.py"
spec = importlib.util.spec_from_file_location("xmanager", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class XManagerTests(unittest.TestCase):
    def test_stop_is_reverse_of_start(self):
        self.assertEqual(mod.STOP_ORDER, list(reversed(mod.START_ORDER)))

    def test_rpm_version_comparison(self):
        self.assertGreater(mod.rpmvercmp("25.10.1-2", "25.10-9"), 0)
        self.assertGreater(mod.rpmvercmp("2.40.0.0-10", "2.40.0.0-2"), 0)
        self.assertEqual(mod.rpmvercmp("25.10-1", "25.10-1"), 0)
        self.assertLess(mod.rpmvercmp("25.10~rc1-1", "25.10-1"), 0)

    def test_ini_release_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Release_25.10.ini"
            path.write_text(
                "[release]\nname=Release_25.10\n\n[packages]\n"
                "modeshape=25.10\nportal=25.10.1\npmc=25.10\n"
                "web=25.10.1\nactivemq=2.40.0.0\n",
                encoding="utf-8",
            )
            name, packages = mod.parse_release_file(path)
        self.assertEqual(name, "Release_25.10")
        self.assertEqual(packages["xout-modeshape"], "25.10")
        self.assertEqual(packages["xout-portal"], "25.10.1")
        self.assertEqual(packages["activemq-artemis-xout"], "2.40.0.0")

    def test_json_release_subset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hotfix.json"
            path.write_text(
                json.dumps({"release": "HF1", "packages": {"web": "25.10.2"}}),
                encoding="utf-8",
            )
            name, packages = mod.parse_release_file(path)
        self.assertEqual(name, "HF1")
        self.assertEqual(packages, {"xout-web": "25.10.2"})

    def test_zypper_xml_parser_and_sort(self):
        xml = """<?xml version='1.0'?>
        <stream><search-result><solvable-list>
          <solvable status='installed' name='xout-web' kind='package' edition='25.10-1' arch='x86_64' repository='System Packages'/>
          <solvable status='not-installed' name='xout-web' kind='package' edition='25.10.1-1' arch='x86_64' repository='xout-rollout-repo'/>
          <solvable status='not-installed' name='xout-web' kind='package' edition='25.10.1-3' arch='x86_64' repository='xout-rollout-repo'/>
          <solvable status='not-installed' name='other' kind='package' edition='99-1' arch='x86_64' repository='xout-rollout-repo'/>
        </solvable-list></search-result></stream>"""
        parsed = mod.parse_zypper_solvables(xml, {"xout-web"})
        versions = mod.sort_editions((x.edition for x in parsed["xout-web"]), reverse=True)
        self.assertEqual(versions, ["25.10.1-3", "25.10.1-1", "25.10-1"])

    def test_build_plan_latest_and_release_subset(self):
        class Dummy:
            args = type("Args", (), {"repo_alias": "xout-repo"})()
            query_repo_candidates = lambda self, packages: {
                "xout-web": [
                    mod.PackageCandidate("xout-web", "25.10-1"),
                    mod.PackageCandidate("xout-web", "25.10.1-3"),
                ],
                "xout-portal": [mod.PackageCandidate("xout-portal", "25.10.1-2")],
            }
            get_installed_package_version = lambda self, package: {
                "xout-web": "25.10-1",
                "xout-portal": "25.10-1",
            }.get(package)
            die = mod.XManager.die
            build_package_plan = mod.XManager.build_package_plan

        dummy = Dummy()
        latest = dummy.build_package_plan(["xout-web", "xout-portal"], None, False)
        self.assertEqual(latest["mode"], "latest")
        self.assertEqual(latest["packages"][0]["target"], "25.10.1-3")
        self.assertEqual(latest["packages"][0]["action"], "upgrade")

        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory) / "hf.ini"
            release.write_text("[packages]\nweb=25.10.1\n", encoding="utf-8")
            targeted = dummy.build_package_plan(["xout-web", "xout-portal"], release, False)
        web, portal = targeted["packages"]
        self.assertEqual(web["action"], "upgrade")
        self.assertEqual(web["target"], "25.10.1-3")
        self.assertEqual(portal["action"], "noop")
        self.assertEqual(portal["reason"], "not_in_release_file")

    def test_downgrade_requires_explicit_flag(self):
        class Dummy:
            args = type("Args", (), {"repo_alias": "xout-repo"})()
            query_repo_candidates = lambda self, packages: {
                "xout-web": [mod.PackageCandidate("xout-web", "25.9-1")]
            }
            get_installed_package_version = lambda self, package: "25.10-1"
            die = mod.XManager.die
            build_package_plan = mod.XManager.build_package_plan

        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory) / "old.ini"
            release.write_text("[packages]\nweb=25.9\n", encoding="utf-8")
            blocked = Dummy().build_package_plan(["xout-web"], release, False)
            allowed = Dummy().build_package_plan(["xout-web"], release, True)
        self.assertEqual(blocked["packages"][0]["action"], "error")
        self.assertEqual(allowed["packages"][0]["action"], "downgrade")

    def test_help_contains_new_commands(self):
        parser = mod.build_parser()
        help_text = parser.format_help()
        for command in (
            "package-plan",
            "package-apply",
            "modules-quiesce",
            "modules-restore",
            "ensure-disabled",
        ):
            self.assertIn(command, help_text)


if __name__ == "__main__":
    unittest.main()
