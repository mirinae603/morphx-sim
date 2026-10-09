import pytest

from morphx.agent.__main__ import parse_args


def test_defaults():
    args = parse_args([])
    assert args.interval == 5.0
    assert args.device_id == "MORPHX_SIM_001"
    assert args.db == "data/MORPHX_SIM_001.sqlite3"


def test_interval_can_be_changed():
    assert parse_args(["--interval", "0.5"]).interval == 0.5


@pytest.mark.parametrize(
    "argv",
    [
        ["--interval", "0"],
        ["--interval", "-2"],
        ["--interval", "nan"],
        ["--interval", "inf"],
        ["--max-backoff", "0"],
        ["--request-timeout", "-1"],
        ["--device-id", "no spaces"],
    ],
)
def test_bad_arguments_are_refused_at_startup(argv):
    with pytest.raises(SystemExit):
        parse_args(argv)
