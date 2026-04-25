import contextlib
import io
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "maint-scripts"
    / "update_dependency_refs.py"
)
VALID_DIGEST = (
    "sha256:0123456789abcdef0123456789abcdef"
    "0123456789abcdef0123456789abcdef"
)


def load_script_module():
    spec = importlib.util.spec_from_file_location(
        "update_dependency_refs", SCRIPT_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load update_dependency_refs.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class UpdateDependencyRefsTests(unittest.TestCase):
    def setUp(self):
        self.module = load_script_module()

    def test_discover_unpinned_images_finds_unpinned_dockerfile_from(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dockerfile = Path(tmpdir) / "Dockerfile"
            dockerfile.write_text(
                "FROM python:3.14-alpine3.23\n", encoding="utf-8"
            )

            unpinned = self.module.discover_unpinned_images((dockerfile,))

        self.assertEqual(len(unpinned), 1)
        self.assertEqual(unpinned[0].line_number, 1)
        self.assertEqual(unpinned[0].ref, "python:3.14-alpine3.23")

    def test_discover_unpinned_images_accepts_digest_pinned_from(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dockerfile = Path(tmpdir) / "Dockerfile"
            dockerfile.write_text(
                f"FROM python:3.14-alpine3.23@{VALID_DIGEST}\n",
                encoding="utf-8",
            )

            unpinned = self.module.discover_unpinned_images((dockerfile,))

        self.assertEqual(unpinned, [])

    def test_discover_unpinned_images_finds_yaml_image_and_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ci = Path(tmpdir) / "ci.yml"
            ci.write_text(
                "\n".join(
                    [
                        "job:",
                        "  image: node:alpine",
                        "  service:",
                        "    name: aquasec/trivy:0.70.0",
                    ]
                ),
                encoding="utf-8",
            )

            unpinned = self.module.discover_unpinned_images((ci,))

        self.assertEqual(
            [item.ref for item in unpinned],
            [
                "node:alpine",
                "aquasec/trivy:0.70.0",
            ],
        )

    def test_discover_unpinned_images_ignores_project_image(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            compose = Path(tmpdir) / "docker-compose.yml"
            compose.write_text(
                "image: registry.gitlab.com/gabriel.chamon/buzz/buzz:v1\n",
                encoding="utf-8",
            )

            unpinned = self.module.discover_unpinned_images((compose,))

        self.assertEqual(unpinned, [])

    def test_check_pins_returns_nonzero_for_unpinned_images(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dockerfile = Path(tmpdir) / "Dockerfile"
            dockerfile.write_text("FROM python:3.14-alpine3.23\n")

            with contextlib.redirect_stderr(io.StringIO()):
                result = self.module.check_pins((dockerfile,))

        self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
