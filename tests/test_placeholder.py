import dps150
from dps150.cli import main


def test_package_imports_and_cli_runs():
    assert dps150.__doc__
    assert main() == 0
