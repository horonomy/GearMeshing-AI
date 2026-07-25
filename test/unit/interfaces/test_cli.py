from typer.testing import CliRunner

from gearmeshing_ai import __version__
from gearmeshing_ai.interfaces.cli import app

runner = CliRunner()


def test_version_command_prints_the_package_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__
