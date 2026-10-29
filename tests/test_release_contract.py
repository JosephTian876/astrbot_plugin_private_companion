"""Release contract: one version, one dependency source, no pinned tests.

These checks are offline and read repository files only. They do not build, install,
start a host, or touch runtime data. The release build and archive verification are
separate commands (``tools/build_v2_release.py`` / ``tools/verify_v2_release.py``).
"""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

from companion_v2.product import PRODUCT_VERSION
from tools.build_v2_release import (
    NATIVE_CAPABILITY_BASELINE,
    ROOT,
    VERSION,
    native_artifact_hashes,
    native_artifact_source_files,
)

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")

# The four attachment parsers promised by requirements.txt. The built package must
# expose these through Requires-Dist instead of relying on the host to preinstall them.
DOCUMENT_PARSERS = {
    "pymupdf": "PyMuPDF>=1.24",
    "python-docx": "python-docx>=1.1",
    "openpyxl": "openpyxl>=3.1",
    "python-pptx": "python-pptx>=0.6.23",
}


def _metadata_version() -> str:
    raw = (ROOT / "metadata.yaml").read_text(encoding="utf-8-sig")
    found = re.search(r"^version:\s*(.+)$", raw, re.MULTILINE)
    if not found:
        raise AssertionError("metadata.yaml has no version field")
    return found.group(1).strip().strip("\"'")


def _pyproject_version() -> str:
    raw = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    found = re.search(r'^version\s*=\s*"([^"]+)"$', raw, re.MULTILINE)
    if not found:
        raise AssertionError("pyproject.toml has no project version")
    return found.group(1)


def _requirements() -> list[str]:
    requirements = []
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            requirements.append(line)
    return requirements


def _requirement_key(line: str) -> str:
    found = REQUIREMENT_NAME.match(line)
    if not found:
        raise AssertionError("unparseable requirement: " + line)
    return re.sub(r"[-_.]+", "-", found.group(1)).lower()


def _is_product_version(node: ast.AST) -> bool:
    return ((isinstance(node, ast.Name) and node.id == "PRODUCT_VERSION")
            or (isinstance(node, ast.Attribute) and node.attr == "PRODUCT_VERSION"))


def _is_semver_literal(node: ast.AST) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and SEMVER.fullmatch(node.value) is not None)


class ReleaseVersionContractTests(unittest.TestCase):
    def test_release_version_is_consistent_across_all_release_metadata(self):
        baseline = json.loads((ROOT / NATIVE_CAPABILITY_BASELINE).read_text(encoding="utf-8"))
        versions = {
            "metadata.yaml": _metadata_version(),
            "pyproject.toml": _pyproject_version(),
            "companion_v2.product.PRODUCT_VERSION": PRODUCT_VERSION,
            "tools.build_v2_release.VERSION": VERSION,
            "tools/native_capability_baseline.json": baseline["package_version"],
        }
        distinct = sorted(set(versions.values()))
        self.assertEqual(len(distinct), 1, versions)
        self.assertIsNotNone(SEMVER.fullmatch(distinct[0]), versions)
        self.assertEqual(baseline["package_version"], versions["metadata.yaml"])

    def test_native_baseline_hashes_match_current_release_sources(self):
        # This compares the pinned artifact descriptors with the bytes that the build
        # hashes from source, so a stale baseline cannot pass by agreeing with itself.
        baseline = json.loads((ROOT / NATIVE_CAPABILITY_BASELINE).read_text(encoding="utf-8"))
        hashes = native_artifact_hashes(native_artifact_source_files(ROOT))
        self.assertEqual(set(hashes),
                         {"content", "game", "image", "live", "reality", "screen", "together"})
        self.assertEqual(baseline["artifact_hashes"], hashes)
        # The 7.0.7 baseline kept the pre-release image hash; keep the image explicit.
        self.assertEqual(baseline["artifact_hashes"]["image"], hashes["image"])

    def test_dependency_metadata_derives_from_requirements_single_source(self):
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
            self.skipTest("tomllib requires Python 3.11+")
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = data["project"]
        self.assertEqual(project.get("dynamic"), ["dependencies"])
        self.assertNotIn("dependencies", project)
        declared = data["tool"]["setuptools"]["dynamic"]["dependencies"]
        self.assertEqual(declared, {"file": ["requirements.txt"]})

        requirements = _requirements()
        keys = [_requirement_key(line) for line in requirements]
        self.assertEqual(len(keys), len(set(keys)), "duplicate requirement in requirements.txt")
        for key, spec in DOCUMENT_PARSERS.items():
            self.assertIn(key, keys, "requirements.txt is missing " + key)
            self.assertIn(spec, requirements, "requirements.txt no longer pins " + spec)

    def test_tests_do_not_pin_the_current_product_version_to_a_literal(self):
        offenders = []
        for path in sorted((ROOT / "tests").rglob("test_*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            except SyntaxError as error:
                self.fail(f"{path.relative_to(ROOT)} does not parse: {error}")
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                name = (function.attr if isinstance(function, ast.Attribute)
                        else function.id if isinstance(function, ast.Name) else "")
                if name not in {"assertEqual", "assertNotEqual"} or len(node.args) != 2:
                    continue
                left, right = node.args
                pinned = ((_is_product_version(left) and _is_semver_literal(right))
                          or (_is_product_version(right) and _is_semver_literal(left)))
                if pinned:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [], "tests must compare PRODUCT_VERSION to computed values, "
                                       "not to a release literal; keep version checks in the "
                                       "release contract: " + ", ".join(offenders))


if __name__ == "__main__":
    unittest.main()
