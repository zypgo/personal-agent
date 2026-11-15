from typer.testing import CliRunner

import personal_agent
from personal_agent.cli import app


def test_version_string():
    assert personal_agent.__version__


def test_cli_version():
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert personal_agent.__version__ in result.output
