import pytest
from pathlib import Path
from petlibro_mcp.config import (
    load_config, cups_to_portions, UnknownPetError, MissingCredentialsError,
)

TOML = str(Path(__file__).parent.parent / "pets.example.toml")
ENV = {"PETLIBRO_EMAIL": "a@b.com", "PETLIBRO_PASSWORD": "pw"}


def test_loads_six_feeders_two_fountains():
    cfg = load_config(TOML, env=ENV)
    assert len(cfg.feeders) == 6
    assert len(cfg.fountains) == 2
    assert cfg.max_cups_per_command == 4
    assert cfg.email == "a@b.com"


def test_resolve_all_returns_every_feeder():
    cfg = load_config(TOML, env=ENV)
    assert {f.name for f in cfg.resolve_feeders("all")} == {
        "zeus", "saffron", "ferris", "ricco", "bridget", "colby"}


def test_resolve_is_case_insensitive():
    cfg = load_config(TOML, env=ENV)
    got = cfg.resolve_feeders(["Ferris", "ZEUS"])
    assert [f.serial for f in got] == [
        "EXAMPLE_FEEDER_SN_FERRIS", "EXAMPLE_FEEDER_SN_ZEUS"]


def test_resolve_unknown_raises_with_valid_names():
    cfg = load_config(TOML, env=ENV)
    with pytest.raises(UnknownPetError) as e:
        cfg.resolve_feeders(["mittens"])
    assert "mittens" in str(e.value)
    assert "zeus" in str(e.value)


def test_cups_to_portions_rounds():
    cfg = load_config(TOML, env=ENV)
    ferris = cfg.resolve_feeders(["ferris"])[0]
    assert cups_to_portions(ferris, 3) == 36
    assert cups_to_portions(ferris, 0.5) == 6


def test_missing_credentials_raises():
    with pytest.raises(MissingCredentialsError):
        load_config(TOML, env={})
