"""sirosid_core must stay a library: no imports from scripts/, no repo-relative paths.

The point of the package is that a hosted service can call it without a checkout
of this repo around it. If a module in here imports a CLI script, or locates a
file by walking up from __file__, that stops being true - quietly, until the
service is deployed somewhere the checkout isn't. These are the two ways it
breaks, so they are checked structurally.
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "sirosid_core"
SCRIPT_MODULES = {p.stem.replace("-", "_") for p in (ROOT / "scripts").glob("*.py")}


class CoreLayering(unittest.TestCase):
    def modules(self):
        return sorted(CORE.glob("*.py"))

    def test_there_are_modules_to_check(self):
        self.assertGreaterEqual(len(self.modules()), 4)

    def test_core_does_not_import_the_cli_scripts(self):
        offenders = []
        for path in self.modules():
            for node in ast.walk(ast.parse(path.read_text())):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                offenders += [f"{path.name}: imports {n}" for n in names if n in SCRIPT_MODULES]
        self.assertEqual(offenders, [])

    def test_core_does_not_depend_on_the_service_above_it(self):
        offenders = []
        for path in self.modules():
            for node in ast.walk(ast.parse(path.read_text())):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                offenders += [f"{path.name}: imports {n}" for n in names if n == "sirosid_service"]
        self.assertEqual(offenders, [])

    def test_core_stays_dependency_light(self):
        """Standard library and PyYAML only: the Machines API client and the
        registry push (machines.py, oci.py) are urllib, not requests/docker SDKs."""
        import sys
        allowed = set(sys.stdlib_module_names) | {"yaml", "sirosid_core"}
        offenders = []
        for path in self.modules():
            for node in ast.walk(ast.parse(path.read_text())):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                offenders += [f"{path.name}: imports {n}" for n in names if n not in allowed]
        self.assertEqual(offenders, [])

    def test_the_single_machine_modules_are_checked(self):
        names = {p.name for p in self.modules()}
        self.assertTrue({"machines.py", "oci.py", "singlemachine.py"} <= names)

    def test_core_does_not_locate_files_relative_to_the_repo(self):
        # Code, not prose: docstrings legitimately explain what is NOT read.
        offenders = []
        for path in self.modules():
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Name) and node.id in ("__file__", "SIROSID_DEV_ROOT"):
                    offenders.append(f"{path.name}:{node.lineno}: uses {node.id}")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                    offenders.append(f"{path.name}:{node.lineno}: calls open()")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
