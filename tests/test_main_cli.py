import os
import subprocess
import sys
import textwrap


def test_self_knowledge_command_does_not_import_api() -> None:
    script = textwrap.dedent(
        """
        import importlib.abc
        import importlib.machinery
        import sys
        import types

        class BlockApiImport(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "cofounder_kernel.api":
                    raise RuntimeError("cofounder_kernel.api should not be imported")
                return None

        fake_self_knowledge_main = types.ModuleType("cofounder_kernel.self_knowledge.__main__")
        fake_self_knowledge_main.run = lambda argv=None: 37
        sys.modules["cofounder_kernel.self_knowledge.__main__"] = fake_self_knowledge_main
        sys.meta_path.insert(0, BlockApiImport())

        import cofounder_kernel.__main__ as kernel_main

        sys.argv = ["cofounder_kernel", "self-knowledge", "--check"]
        kernel_main.main()
        """
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.abspath("src")

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 37, completed.stderr
