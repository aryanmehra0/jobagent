"""Every CLI command must at least load and describe itself; the cheap ones must run on an empty workspace."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from job_agent.cli import cli


def _names(group):
    for name, command in group.commands.items():
        yield name
        if hasattr(command, "commands"):
            for child in command.commands:
                yield f"{name} {child}"


@pytest.mark.parametrize("command", sorted(_names(cli)))
def test_every_command_prints_help(command):
    result = CliRunner().invoke(cli, [*command.split(), "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_status_runs_on_an_empty_workspace():
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0, result.output


def test_verify_fails_cleanly_without_a_profile():
    result = CliRunner().invoke(cli, ["verify"])
    # A missing profile is a normal first-run state: a message and a non-zero exit, not a traceback.
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)


def test_the_version_flag_works():
    assert CliRunner().invoke(cli, ["--version"]).exit_code == 0
