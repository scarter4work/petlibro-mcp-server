# PetLibro MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an MCP server that lets Claude feed 6 PetLibro RFID feeders, open their lids, and read feeder/fountain status via natural language.

**Architecture:** Thin structured MCP tools → a clean async `PetLibroClient` facade → a vendored, reverse-engineered `PetLibroAPI` cloud client (ported from the `cd1zz/petlibro-homeassistant` HA integration, HA coupling stripped). A `pets.toml` file maps pet names ↔ device serials ↔ RFID chips and holds cups→portions calibration. Natural-language parsing lives in Claude, not the server — tools are typed and structured.

**Tech Stack:** Python ≥3.10, `mcp` SDK (stdio transport), `aiohttp`, `async-timeout`, `tomllib` (stdlib), `pytest` + `pytest-asyncio`, `hatchling` build. Mirrors the existing `deco-mcp-server` layout.

## Global Constraints

- Python `>=3.10`; `mcp>=1.9.0`, `aiohttp>=3.9`, `async-timeout>=4.0`.
- **No secrets in git.** Credentials come from env (`PETLIBRO_EMAIL`, `PETLIBRO_PASSWORD`, optional `PETLIBRO_REGION`), loaded from a git-ignored `.env`. `pets.toml` contains serials/MACs/chips only — no passwords.
- **No silent fallbacks.** Auth failures, unknown pet names, and per-device API errors surface loudly with the real message/code — never swallowed into a generic success.
- **Overfeed guard:** `feed` refuses any per-feeder amount above `max_cups_per_command` (default 4 cups) unless `force=true`.
- **Portion conversion:** `portions_per_cup` default 12 (global), overridable per feeder in `pets.toml`. Calibrated by the owner after build.
- Region base URLs from the vendored client: `US -> https://api.us.petlibro.com`. Fixed app creds `APPID=1`, `APPSN="c35772530d1041699c87fe62348507a8"`.
- Feed API unit is integer `grainNum` ("portions"). `requestId = uuid.uuid4().hex`.
- The repo is already git-initialized at `~/projects/petlibro` with `.gitignore` (`.venv/ __pycache__/ *.pyc .pytest_cache/ scripts/smoke.py .env`) and the design spec committed. Work from there.

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `src/petlibro_mcp/__init__.py`
- Create: `src/petlibro_mcp/vendored/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/test_smoke.py`

**Interfaces:**
- Produces: an installable `petlibro_mcp` package and a working pytest setup that all later tasks build on.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "petlibro-mcp-server"
version = "0.1.0"
description = "MCP server for PetLibro RFID feeders and Dockstream fountains."
readme = "README.md"
requires-python = ">=3.10"
license = { text = "MIT" }
authors = [{ name = "scarter4work" }]
dependencies = [
  "mcp>=1.9.0",
  "aiohttp>=3.9",
  "async-timeout>=4.0",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[project.scripts]
petlibro-mcp = "petlibro_mcp.server:main"

[tool.hatch.build.targets.wheel]
packages = ["src/petlibro_mcp"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["src"]
```

- [ ] **Step 2: Create the package `__init__.py` files**

`src/petlibro_mcp/__init__.py`:
```python
"""PetLibro MCP server."""
__version__ = "0.1.0"
```

`src/petlibro_mcp/vendored/__init__.py`:
```python
"""Vendored PetLibro cloud API client (ported from cd1zz/petlibro-homeassistant)."""
```

`tests/__init__.py`:
```python
```

- [ ] **Step 3: Write a smoke test**

`tests/test_smoke.py`:
```python
import petlibro_mcp


def test_package_imports():
    assert petlibro_mcp.__version__ == "0.1.0"
```

- [ ] **Step 4: Create venv, install, run the smoke test**

Run:
```bash
cd ~/projects/petlibro && python3 -m venv .venv && \
  .venv/bin/pip install -e ".[dev]" && \
  .venv/bin/pytest -q
```
Expected: 1 passed.

- [ ] **Step 5: Create a `.env` reminder is unnecessary — `.env` already exists (git-ignored). Commit the scaffold**

```bash
cd ~/projects/petlibro && git add pyproject.toml src tests && \
  git commit -m "feat: project scaffold and package skeleton"
```

---

### Task 2: Vendor the PetLibro cloud API client

The HA integration's `api.py` (61 KB) contains `PetLibroSession` + `PetLibroAPI` (what we need) and a `PetLibroDataCoordinator` (HA-coupled, drop it). We copy the file, strip Home Assistant imports and the coordinator, and keep the two client classes plus their helpers.

**Files:**
- Create: `src/petlibro_mcp/vendored/api.py` (downloaded + trimmed)
- Create: `src/petlibro_mcp/vendored/exceptions.py` (downloaded)
- Create: `tests/test_vendored.py`

**Interfaces:**
- Produces: `PetLibroAPI` with the async methods the facade calls:
  - `login(email: str, password: str) -> str`
  - `list_devices() -> list[dict]`
  - `get_device_real_info(device_id: str) -> dict`
  - `set_manual_feed(serial: str, feed_value=1) -> Any`
  - `set_manual_lid_open(serial: str) -> Any`
  - module-level `hash_password(password: str) -> str` (MD5 hex)
- Produces: `PetLibroSession` (constructed by `PetLibroAPI`; wraps an `aiohttp.ClientSession`).
- Produces: exceptions `PetLibroError`, `PetLibroAPIError`, `PetLibroInvalidAuth` (names confirmed while porting).

- [ ] **Step 1: Download the two source files**

```bash
cd ~/projects/petlibro
BASE=https://raw.githubusercontent.com/cd1zz/petlibro-homeassistant/0c1d6765d68c73992a9e5d50e692d39be14781e3/custom_components/petlibro
curl -fsSL "$BASE/api.py" -o src/petlibro_mcp/vendored/api.py
curl -fsSL "$BASE/exceptions.py" -o src/petlibro_mcp/vendored/exceptions.py
```

- [ ] **Step 2: Strip Home Assistant coupling from `api.py`**

Edit `src/petlibro_mcp/vendored/api.py`:
1. Delete every `from homeassistant...` / `import homeassistant...` line.
2. Delete the entire `class PetLibroDataCoordinator(...)` block (it subclasses HA's `DataUpdateCoordinator` and is unused).
3. If `PetLibroSession.__init__` takes an `aiohttp.ClientSession` from HA (`async_get_clientsession`), change it so the session is created internally: keep a parameter `websession: aiohttp.ClientSession | None = None` and, when `None`, lazily create `aiohttp.ClientSession()` on first use. Add a `async def close(self)` that closes an internally-created session.
4. Fix any now-dangling imports (e.g. relative `from .exceptions import ...` stays; `from .const import ...` — if referenced constants exist, copy them inline or create `vendored/const.py` with just those names).

Iterate: run the import check in Step 3 and fix each `ImportError`/`ModuleNotFoundError` by removing or inlining the offending HA-coupled reference until it imports cleanly.

- [ ] **Step 3: Verify it imports standalone**

Run:
```bash
cd ~/projects/petlibro && .venv/bin/python -c \
  "from petlibro_mcp.vendored.api import PetLibroAPI, PetLibroSession, hash_password; print('ok')"
```
Expected: `ok` (no traceback). If `hash_password` is a method rather than module function, note its real location for Task 4 and adjust the import.

- [ ] **Step 4: Write a pure-function test**

`tests/test_vendored.py`:
```python
import hashlib
from petlibro_mcp.vendored.api import hash_password


def test_hash_password_is_md5_hex():
    assert hash_password("secret") == hashlib.md5(b"secret").hexdigest()
```

- [ ] **Step 5: Run it**

Run: `cd ~/projects/petlibro && .venv/bin/pytest tests/test_vendored.py -q`
Expected: 1 passed. (If `hash_password` lives on the class, import it from its real path and adjust the assertion accordingly.)

- [ ] **Step 6: Commit**

```bash
cd ~/projects/petlibro && git add src/petlibro_mcp/vendored tests/test_vendored.py && \
  git commit -m "feat: vendor PetLibro cloud API client (HA coupling stripped)"
```

---

### Task 3: Config loader (`config.py`)

**Files:**
- Create: `src/petlibro_mcp/config.py`
- Create: `pets.toml`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `@dataclass Feeder(name: str, serial: str, mac: str, chip: str, portions_per_cup: int)`
  - `@dataclass Fountain(name: str, serial: str, mac: str, near: list[str])`
  - `@dataclass Config(feeders: list[Feeder], fountains: list[Fountain], region: str, max_cups_per_command: int, email: str, password: str)`
  - `load_config(toml_path: str | Path, env: Mapping[str, str] = os.environ) -> Config`
  - `Config.resolve_feeders(names: list[str] | str) -> list[Feeder]` — accepts a list of names or the string `"all"`; case-insensitive; raises `UnknownPetError(name, valid_names)` on miss.
  - `Config.resolve_fountains(name: str | None) -> list[Fountain]`
  - `cups_to_portions(feeder: Feeder, cups: float) -> int` — `round(cups * feeder.portions_per_cup)`.
  - Exceptions `UnknownPetError`, `MissingCredentialsError`.

- [ ] **Step 1: Write `pets.toml` with the real inventory**

```toml
[defaults]
portions_per_cup = 12
region = "US"
max_cups_per_command = 4

[[pet]]
name = "zeus"
serial = "EXAMPLE_FEEDER_SN_ZEUS"
mac = "EXAMPLE_MAC_ZEUS"
chip = "100001"

[[pet]]
name = "saffron"
serial = "EXAMPLE_FEEDER_SN_SAFFRON"
mac = "EXAMPLE_MAC_SAFFRON"
chip = "100002"

[[pet]]
name = "ferris"
serial = "EXAMPLE_FEEDER_SN_FERRIS"
mac = "EXAMPLE_MAC_FERRIS"
chip = "100003"

[[pet]]
name = "ricco"
serial = "EXAMPLE_FEEDER_SN_RICCO"
mac = "EXAMPLE_MAC_RICCO"
chip = "100004"

[[pet]]
name = "bridget"
serial = "EXAMPLE_FEEDER_SN_BRIDGET"
mac = "EXAMPLE_MAC_BRIDGET"
chip = "100005"

[[pet]]
name = "colby"
serial = "EXAMPLE_FEEDER_SN_COLBY"
mac = "EXAMPLE_MAC_COLBY"
chip = "100006"

[[fountain]]
name = "dockstream1"
serial = "EXAMPLE_FOUNTAIN_SN_1"
mac = "EXAMPLE_MAC_FOUNTAIN_1"
near = ["zeus", "colby", "bridget"]

[[fountain]]
name = "dockstream2"
serial = "EXAMPLE_FOUNTAIN_SN_2"
mac = "EXAMPLE_MAC_FOUNTAIN_2"
near = ["saffron", "ricco", "ferris", "colby"]
```

- [ ] **Step 2: Write failing tests**

`tests/test_config.py`:
```python
import pytest
from pathlib import Path
from petlibro_mcp.config import (
    load_config, cups_to_portions, UnknownPetError, MissingCredentialsError,
)

TOML = str(Path(__file__).parent.parent / "pets.toml")
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd ~/projects/petlibro && .venv/bin/pytest tests/test_config.py -q`
Expected: FAIL / errors (`config` module or names not defined).

- [ ] **Step 4: Implement `config.py`**

```python
"""Load pets.toml + env credentials; resolve names to devices."""
from __future__ import annotations
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


class UnknownPetError(Exception):
    def __init__(self, name: str, valid: list[str]):
        super().__init__(
            f"Unknown pet '{name}'. Valid pets: {', '.join(sorted(valid))}"
        )
        self.name = name
        self.valid = valid


class MissingCredentialsError(Exception):
    pass


@dataclass
class Feeder:
    name: str
    serial: str
    mac: str
    chip: str
    portions_per_cup: int


@dataclass
class Fountain:
    name: str
    serial: str
    mac: str
    near: list[str] = field(default_factory=list)


@dataclass
class Config:
    feeders: list[Feeder]
    fountains: list[Fountain]
    region: str
    max_cups_per_command: int
    email: str
    password: str

    def _by_name(self) -> dict[str, Feeder]:
        return {f.name.lower(): f for f in self.feeders}

    def resolve_feeders(self, names) -> list[Feeder]:
        if isinstance(names, str) and names.lower() == "all":
            return list(self.feeders)
        if isinstance(names, str):
            names = [names]
        index = self._by_name()
        valid = [f.name for f in self.feeders]
        out = []
        for n in names:
            key = n.lower()
            if key not in index:
                raise UnknownPetError(n, valid)
            out.append(index[key])
        return out

    def resolve_fountains(self, name=None) -> list[Fountain]:
        if name is None:
            return list(self.fountains)
        for f in self.fountains:
            if f.name.lower() == name.lower():
                return [f]
        raise UnknownPetError(name, [f.name for f in self.fountains])


def cups_to_portions(feeder: Feeder, cups: float) -> int:
    return round(cups * feeder.portions_per_cup)


def load_config(toml_path, env: Mapping[str, str] = os.environ) -> Config:
    data = tomllib.loads(Path(toml_path).read_text())
    defaults = data.get("defaults", {})
    ppc = int(defaults.get("portions_per_cup", 12))
    region = defaults.get("region", "US")
    max_cups = int(defaults.get("max_cups_per_command", 4))

    feeders = [
        Feeder(
            name=p["name"], serial=p["serial"], mac=p.get("mac", ""),
            chip=str(p.get("chip", "")),
            portions_per_cup=int(p.get("portions_per_cup", ppc)),
        )
        for p in data.get("pet", [])
    ]
    fountains = [
        Fountain(name=f["name"], serial=f["serial"], mac=f.get("mac", ""),
                 near=list(f.get("near", [])))
        for f in data.get("fountain", [])
    ]

    email = env.get("PETLIBRO_EMAIL")
    password = env.get("PETLIBRO_PASSWORD")
    if not email or not password:
        raise MissingCredentialsError(
            "Set PETLIBRO_EMAIL and PETLIBRO_PASSWORD (see .env)."
        )
    region = env.get("PETLIBRO_REGION", region)

    return Config(feeders, fountains, region, max_cups, email, password)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/projects/petlibro && .venv/bin/pytest tests/test_config.py -q`
Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
cd ~/projects/petlibro && git add src/petlibro_mcp/config.py pets.toml tests/test_config.py && \
  git commit -m "feat: config loader with pet/device map and cups->portions"
```

---

### Task 4: Client facade (`client.py`)

Wraps the vendored `PetLibroAPI` with the exact operations the tools need, returning small typed results. Logs in lazily on first call.

**Files:**
- Create: `src/petlibro_mcp/client.py`
- Create: `tests/test_client.py`

**Interfaces:**
- Consumes: `PetLibroAPI` (Task 2), `Config` (Task 3).
- Produces:
  - `class PetLibroClient` with async methods:
    - `async ensure_login() -> None`
    - `async list_devices() -> list[dict]`
    - `async feed(serial: str, portions: int) -> None`
    - `async open_lid(serial: str) -> None`
    - `async real_info(serial: str) -> dict`
  - Constructed as `PetLibroClient(config: Config, api: PetLibroAPI | None = None)` — `api` injectable for tests.

- [ ] **Step 1: Write failing tests with a mocked api**

`tests/test_client.py`:
```python
import pytest
from unittest.mock import AsyncMock
from petlibro_mcp.client import PetLibroClient
from petlibro_mcp.config import Config, Feeder


def make_config():
    return Config(
        feeders=[Feeder("ferris", "SN-FERRIS", "", "", 12)],
        fountains=[], region="US", max_cups_per_command=4,
        email="a@b.com", password="pw",
    )


@pytest.fixture
def api():
    m = AsyncMock()
    m.login = AsyncMock(return_value="token123")
    m.set_manual_feed = AsyncMock(return_value={"code": 0})
    m.set_manual_lid_open = AsyncMock(return_value={"code": 0})
    m.get_device_real_info = AsyncMock(return_value={"surplusGrain": True})
    m.list_devices = AsyncMock(return_value=[{"deviceSn": "SN-FERRIS"}])
    return m


async def test_ensure_login_calls_api_once(api):
    c = PetLibroClient(make_config(), api=api)
    await c.ensure_login()
    await c.ensure_login()
    api.login.assert_awaited_once_with("a@b.com", "pw")


async def test_feed_logs_in_then_dispenses(api):
    c = PetLibroClient(make_config(), api=api)
    await c.feed("SN-FERRIS", 36)
    api.login.assert_awaited_once()
    api.set_manual_feed.assert_awaited_once_with("SN-FERRIS", 36)


async def test_open_lid_calls_api(api):
    c = PetLibroClient(make_config(), api=api)
    await c.open_lid("SN-FERRIS")
    api.set_manual_lid_open.assert_awaited_once_with("SN-FERRIS")


async def test_real_info_passthrough(api):
    c = PetLibroClient(make_config(), api=api)
    assert await c.real_info("SN-FERRIS") == {"surplusGrain": True}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/projects/petlibro && .venv/bin/pytest tests/test_client.py -q`
Expected: FAIL (`client` module not found).

- [ ] **Step 3: Implement `client.py`**

```python
"""Clean async facade over the vendored PetLibro cloud client."""
from __future__ import annotations
from .config import Config
from .vendored.api import PetLibroAPI


class PetLibroClient:
    def __init__(self, config: Config, api: PetLibroAPI | None = None):
        self._config = config
        # PetLibroAPI construction args are confirmed while wiring Task 2's
        # stripped client; region selects the base URL. If the real __init__
        # needs a session, pass None to let it create one lazily.
        self._api = api or PetLibroAPI(region=config.region)
        self._logged_in = False

    async def ensure_login(self) -> None:
        if not self._logged_in:
            await self._api.login(self._config.email, self._config.password)
            self._logged_in = True

    async def list_devices(self) -> list[dict]:
        await self.ensure_login()
        return await self._api.list_devices()

    async def feed(self, serial: str, portions: int) -> None:
        await self.ensure_login()
        await self._api.set_manual_feed(serial, portions)

    async def open_lid(self, serial: str) -> None:
        await self.ensure_login()
        await self._api.set_manual_lid_open(serial)

    async def real_info(self, serial: str) -> dict:
        await self.ensure_login()
        return await self._api.get_device_real_info(serial)
```

Note for the implementer: adjust the `PetLibroAPI(...)` constructor call in `__init__` to match the real signature exposed by the trimmed Task 2 client (e.g. it may be `PetLibroAPI(region=...)` or require a session/base_url). The tests inject a mock `api`, so they pass regardless; the constructor line is exercised only in the live smoke test (Task 7).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/projects/petlibro && .venv/bin/pytest tests/test_client.py -q`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/petlibro && git add src/petlibro_mcp/client.py tests/test_client.py && \
  git commit -m "feat: PetLibroClient facade over vendored API"
```

---

### Task 5: MCP tools (`tools.py`)

Pure orchestration: resolve names, enforce the overfeed guard, call the facade, shape per-device results. No API knowledge.

**Files:**
- Create: `src/petlibro_mcp/tools.py`
- Create: `tests/test_tools.py`

**Interfaces:**
- Consumes: `PetLibroClient` (Task 4), `Config`, `cups_to_portions`, `UnknownPetError` (Task 3).
- Produces (all take `config` + `client`, return JSON-serializable dicts/lists):
  - `async feed(config, client, pets, cups: float, force: bool = False) -> list[dict]`
    result items: `{"pet","cups","portions","ok","error"}`. On overfeed with `force=False`, returns a single `{"ok": False, "error": "..."}` naming offenders and dispenses nothing.
  - `async open_lid(config, client, pets) -> list[dict]` items `{"pet","ok","error"}`.
  - `async feeder_status(config, client, pet=None) -> list[dict]` items `{"pet","serial","online","food_low","battery","today_feeding_quantity","raw"}`.
  - `async fountain_status(config, client, name=None) -> list[dict]` items `{"fountain","serial","near","raw"}`.
  - `async list_devices(config, client) -> dict` `{"feeders":[...],"fountains":[...],"cloud_devices":[...]}`.

- [ ] **Step 1: Write failing tests**

`tests/test_tools.py`:
```python
import pytest
from unittest.mock import AsyncMock
from petlibro_mcp import tools
from petlibro_mcp.config import Config, Feeder, Fountain


def cfg():
    return Config(
        feeders=[Feeder("ferris", "SN-F", "", "", 12),
                 Feeder("zeus", "SN-Z", "", "", 12)],
        fountains=[Fountain("dockstream1", "WF-1", "", ["zeus"])],
        region="US", max_cups_per_command=4, email="a@b.com", password="pw",
    )


@pytest.fixture
def client():
    m = AsyncMock()
    m.feed = AsyncMock(return_value=None)
    m.open_lid = AsyncMock(return_value=None)
    m.real_info = AsyncMock(return_value={
        "online": True, "surplusGrain": True, "batteryState": "high"})
    return m


async def test_feed_single_pet_converts_and_dispenses(client):
    res = await tools.feed(cfg(), client, ["ferris"], 3)
    assert res == [{"pet": "ferris", "cups": 3, "portions": 36,
                    "ok": True, "error": None}]
    client.feed.assert_awaited_once_with("SN-F", 36)


async def test_feed_all_hits_every_feeder(client):
    res = await tools.feed(cfg(), client, "all", 1)
    assert {r["pet"] for r in res} == {"ferris", "zeus"}
    assert client.feed.await_count == 2


async def test_feed_overfeed_refused_without_force(client):
    res = await tools.feed(cfg(), client, ["ferris"], 10)
    assert res[0]["ok"] is False
    assert "10" in res[0]["error"] and "ferris" in res[0]["error"]
    client.feed.assert_not_awaited()


async def test_feed_overfeed_allowed_with_force(client):
    res = await tools.feed(cfg(), client, ["ferris"], 10, force=True)
    assert res[0]["ok"] is True
    client.feed.assert_awaited_once()


async def test_feed_unknown_pet_reports_error(client):
    res = await tools.feed(cfg(), client, ["mittens"], 1)
    assert res[0]["ok"] is False
    assert "mittens" in res[0]["error"]
    client.feed.assert_not_awaited()


async def test_feed_partial_failure_is_reported(client):
    client.feed = AsyncMock(side_effect=[None, RuntimeError("offline")])
    res = await tools.feed(cfg(), client, ["ferris", "zeus"], 1)
    oks = {r["pet"]: r["ok"] for r in res}
    assert oks == {"ferris": True, "zeus": False}
    assert any("offline" in (r["error"] or "") for r in res)


async def test_open_lid_all(client):
    res = await tools.open_lid(cfg(), client, "all")
    assert {r["pet"] for r in res} == {"ferris", "zeus"}
    assert client.open_lid.await_count == 2


async def test_feeder_status_single(client):
    res = await tools.feeder_status(cfg(), client, "ferris")
    assert res[0]["pet"] == "ferris"
    assert res[0]["online"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/projects/petlibro && .venv/bin/pytest tests/test_tools.py -q`
Expected: FAIL (`tools` not found).

- [ ] **Step 3: Implement `tools.py`**

```python
"""MCP tool logic: resolve, guard, call the facade, shape results."""
from __future__ import annotations
from .config import Config, cups_to_portions, UnknownPetError
from .client import PetLibroClient


async def feed(config: Config, client: PetLibroClient, pets, cups: float,
               force: bool = False) -> list[dict]:
    try:
        feeders = config.resolve_feeders(pets)
    except UnknownPetError as e:
        return [{"pet": None, "cups": cups, "portions": None,
                 "ok": False, "error": str(e)}]

    if not force and cups > config.max_cups_per_command:
        offenders = ", ".join(f.name for f in feeders)
        return [{"pet": None, "cups": cups, "portions": None, "ok": False,
                 "error": (f"Refusing {cups} cups (> {config.max_cups_per_command} "
                           f"cap) for {offenders}. Pass force=true to override.")}]

    results = []
    for f in feeders:
        portions = cups_to_portions(f, cups)
        item = {"pet": f.name, "cups": cups, "portions": portions,
                "ok": True, "error": None}
        try:
            await client.feed(f.serial, portions)
        except Exception as exc:  # surface, never swallow
            item["ok"] = False
            item["error"] = f"{type(exc).__name__}: {exc}"
        results.append(item)
    return results


async def open_lid(config: Config, client: PetLibroClient, pets) -> list[dict]:
    try:
        feeders = config.resolve_feeders(pets)
    except UnknownPetError as e:
        return [{"pet": None, "ok": False, "error": str(e)}]
    results = []
    for f in feeders:
        item = {"pet": f.name, "ok": True, "error": None}
        try:
            await client.open_lid(f.serial)
        except Exception as exc:
            item["ok"] = False
            item["error"] = f"{type(exc).__name__}: {exc}"
        results.append(item)
    return results


async def feeder_status(config: Config, client: PetLibroClient,
                        pet=None) -> list[dict]:
    feeders = config.resolve_feeders("all" if pet is None else [pet])
    results = []
    for f in feeders:
        try:
            info = await client.real_info(f.serial)
            results.append({
                "pet": f.name, "serial": f.serial,
                "online": info.get("online"),
                "food_low": info.get("surplusGrain"),
                "battery": info.get("batteryState"),
                "today_feeding_quantity": info.get("todayFeedingQuantity"),
                "raw": info,
            })
        except Exception as exc:
            results.append({"pet": f.name, "serial": f.serial,
                            "error": f"{type(exc).__name__}: {exc}"})
    return results


async def fountain_status(config: Config, client: PetLibroClient,
                          name=None) -> list[dict]:
    fountains = config.resolve_fountains(name)
    results = []
    for f in fountains:
        try:
            info = await client.real_info(f.serial)
            results.append({"fountain": f.name, "serial": f.serial,
                            "near": f.near, "raw": info})
        except Exception as exc:
            results.append({"fountain": f.name, "serial": f.serial,
                            "error": f"{type(exc).__name__}: {exc}"})
    return results


async def list_devices(config: Config, client: PetLibroClient) -> dict:
    cloud = []
    try:
        cloud = await client.list_devices()
    except Exception as exc:
        cloud = [{"error": f"{type(exc).__name__}: {exc}"}]
    return {
        "feeders": [{"pet": f.name, "serial": f.serial} for f in config.feeders],
        "fountains": [{"name": f.name, "serial": f.serial} for f in config.fountains],
        "cloud_devices": cloud,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/projects/petlibro && .venv/bin/pytest tests/test_tools.py -q`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/petlibro && git add src/petlibro_mcp/tools.py tests/test_tools.py && \
  git commit -m "feat: MCP tool logic (feed/open_lid/status) with overfeed guard"
```

---

### Task 6: MCP server (`server.py`)

Registers the five tools with the MCP SDK over stdio, wiring each to `tools.py` with a shared `Config` + `PetLibroClient`.

**Files:**
- Create: `src/petlibro_mcp/server.py`
- Create: `tests/test_server.py`

**Interfaces:**
- Consumes: `tools` (Task 5), `load_config` (Task 3), `PetLibroClient` (Task 4).
- Produces: `def main() -> None` (console-script entry point); `def build_server(config, client) -> Server` returning a configured MCP `Server` for testing.

- [ ] **Step 1: Write a test that the server registers all five tools**

`tests/test_server.py`:
```python
import pytest
from unittest.mock import AsyncMock
from petlibro_mcp.server import build_server
from petlibro_mcp.config import Config


def cfg():
    return Config(feeders=[], fountains=[], region="US",
                  max_cups_per_command=4, email="a@b.com", password="pw")


async def test_lists_five_tools():
    server = build_server(cfg(), AsyncMock())
    handler = server.request_handlers  # sanity: server built
    tools = await server.list_tools()  # MCP Server exposes registered tools
    names = {t.name for t in tools}
    assert names == {"feed", "open_lid", "feeder_status",
                     "fountain_status", "list_devices"}
```

Note: the exact call to enumerate tools depends on the installed `mcp` SDK version. If `server.list_tools()` isn't directly awaitable, adjust to the SDK's decorator/registry introspection (e.g. inspect the registered `@server.list_tools()` handler). The intent — assert all five names are registered — stays the same.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/projects/petlibro && .venv/bin/pytest tests/test_server.py -q`
Expected: FAIL (`server` not found).

- [ ] **Step 3: Implement `server.py`**

```python
"""MCP server exposing PetLibro feeder/fountain tools over stdio."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from . import tools as T
from .client import PetLibroClient
from .config import load_config

PETS_TOML = os.environ.get(
    "PETLIBRO_PETS_TOML",
    str(Path(__file__).resolve().parent.parent.parent / "pets.toml"),
)

TOOL_DEFS = [
    Tool(
        name="feed",
        description=("Dispense food. 'pets' is a list of pet names or \"all\". "
                     "'cups' is cups of food; refused above the overfeed cap "
                     "unless force=true."),
        inputSchema={
            "type": "object",
            "properties": {
                "pets": {"oneOf": [
                    {"type": "array", "items": {"type": "string"}},
                    {"type": "string"}]},
                "cups": {"type": "number"},
                "force": {"type": "boolean", "default": False},
            },
            "required": ["pets", "cups"],
        },
    ),
    Tool(
        name="open_lid",
        description="Force the RFID lid open. 'pets' is a list of names or \"all\".",
        inputSchema={
            "type": "object",
            "properties": {"pets": {"oneOf": [
                {"type": "array", "items": {"type": "string"}},
                {"type": "string"}]}},
            "required": ["pets"],
        },
    ),
    Tool(
        name="feeder_status",
        description="Feeder status (food/battery/online/today). Omit 'pet' for all.",
        inputSchema={"type": "object",
                     "properties": {"pet": {"type": "string"}}},
    ),
    Tool(
        name="fountain_status",
        description="Fountain status. Omit 'name' for all fountains.",
        inputSchema={"type": "object",
                     "properties": {"name": {"type": "string"}}},
    ),
    Tool(
        name="list_devices",
        description="List configured feeders/fountains plus live cloud devices.",
        inputSchema={"type": "object", "properties": {}},
    ),
]


def build_server(config, client: PetLibroClient) -> Server:
    server = Server("petlibro-mcp")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return TOOL_DEFS

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        a = arguments or {}
        if name == "feed":
            result = await T.feed(config, client, a["pets"], a["cups"],
                                  a.get("force", False))
        elif name == "open_lid":
            result = await T.open_lid(config, client, a["pets"])
        elif name == "feeder_status":
            result = await T.feeder_status(config, client, a.get("pet"))
        elif name == "fountain_status":
            result = await T.fountain_status(config, client, a.get("name"))
        elif name == "list_devices":
            result = await T.list_devices(config, client)
        else:
            raise ValueError(f"Unknown tool: {name}")
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    return server


async def _run() -> None:
    config = load_config(PETS_TOML)
    client = PetLibroClient(config)
    server = build_server(config, client)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_run())
```

- [ ] **Step 4: Run the test; adjust the enumeration call to the SDK if needed**

Run: `cd ~/projects/petlibro && .venv/bin/pytest tests/test_server.py -q`
Expected: PASS. If the SDK doesn't expose `list_tools()` for direct await, call the registered handler directly (e.g. `await server._list_tools_handler()` or inspect `server.list_tools` registry per the installed version) — assert the five names.

- [ ] **Step 5: Verify the server process starts and lists tools over stdio**

Run:
```bash
cd ~/projects/petlibro && PETLIBRO_EMAIL=x PETLIBRO_PASSWORD=y \
  timeout 3 .venv/bin/petlibro-mcp </dev/null; echo "exit=$?"
```
Expected: starts, waits for stdio, exits on EOF/timeout without import errors (`exit=124` from timeout is fine).

- [ ] **Step 6: Commit**

```bash
cd ~/projects/petlibro && git add src/petlibro_mcp/server.py tests/test_server.py && \
  git commit -m "feat: MCP server wiring five PetLibro tools over stdio"
```

---

### Task 7: Live smoke test, README, and Claude registration

Verify the whole stack against the real account (login + `list_devices`), document setup, and register the server with Claude. This task's deliverable is a working end-to-end connection and docs — no new library code.

**Files:**
- Create: `scripts/smoke.py` (git-ignored)
- Create: `README.md`
- Modify: user's Claude MCP config (`.mcp.json` or Claude Desktop config)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the smoke script**

`scripts/smoke.py`:
```python
"""Live smoke test against the real PetLibro account. Reads .env."""
import asyncio, os
from pathlib import Path
from petlibro_mcp.config import load_config
from petlibro_mcp.client import PetLibroClient

# minimal .env loader (no dependency)
for line in Path(__file__).resolve().parent.parent.joinpath(".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


async def main():
    cfg = load_config(str(Path(__file__).resolve().parent.parent / "pets.toml"))
    client = PetLibroClient(cfg)
    await client.ensure_login()
    print("login OK")
    devices = await client.list_devices()
    print(f"cloud reports {len(devices)} devices")
    for d in devices:
        print(" -", d.get("deviceSn"), d.get("deviceName") or d.get("name"))
    # match cloud serials to our config
    cloud_serials = {d.get("deviceSn") for d in devices}
    for f in cfg.feeders + cfg.fountains:
        mark = "OK" if f.serial in cloud_serials else "MISSING from cloud"
        print(f"   config {f.name} {f.serial}: {mark}")


asyncio.run(main())
```

- [ ] **Step 2: Run the smoke test**

Run: `cd ~/projects/petlibro && .venv/bin/python scripts/smoke.py`
Expected: `login OK`, a device count, and each configured serial marked `OK`.
- If login fails: verify `.env` creds; check the region base URL in the vendored client.
- If serials show `MISSING`: the cloud may report a different serial form than the label sticker — note the cloud serials and reconcile `pets.toml` (this is exactly why we smoke-test before trusting feed calls).

- [ ] **Step 3: (Owner-gated) verify one real feed + lid on a single feeder**

Only after Step 2 is clean, and with the owner watching the physical feeder, test the smallest real action on ONE feeder (e.g. ferris, 1 portion). This is a manual, supervised check — do not batch or `feed all` during verification.

Run:
```bash
cd ~/projects/petlibro && .venv/bin/python -c "
import asyncio; from scripts.smoke import *  # loads .env
from petlibro_mcp.config import load_config
from petlibro_mcp.client import PetLibroClient
async def go():
    cfg = load_config('pets.toml'); c = PetLibroClient(cfg)
    f = cfg.resolve_feeders(['ferris'])[0]
    await c.feed(f.serial, 1); print('fed 1 portion')
    await c.open_lid(f.serial); print('lid opened')
asyncio.run(go())"
```
Expected: the physical feeder dispenses one portion and the lid opens. Confirms `grainNum`/`doorStateChange` payloads work on this hardware.

- [ ] **Step 4: Write `README.md`**

Document: what it is, install (`python -m venv`, `pip install -e .`), `.env` setup, `pets.toml`, calibrating `portions_per_cup` (dispense N portions, measure cups, set ratio), the five tools with example prompts ("feed ferris 3 cups", "open all the lids", "how much food does zeus have"), and the Claude MCP registration snippet from Step 5.

- [ ] **Step 5: Register with Claude**

Add to the user's MCP config (`~/projects/.mcp.json` for Claude Code, or Claude Desktop config):
```json
{
  "mcpServers": {
    "petlibro": {
      "command": "/home/scarter4work/projects/petlibro/.venv/bin/petlibro-mcp",
      "env": {
        "PETLIBRO_EMAIL": "you@example.com",
        "PETLIBRO_PASSWORD": "REDACTED",
        "PETLIBRO_PETS_TOML": "/home/scarter4work/projects/petlibro/pets.toml"
      }
    }
  }
}
```
(Prefer pointing at `.env` if the harness supports it; otherwise this config file must itself be kept out of git.)

- [ ] **Step 6: Commit (README only — smoke.py and any secret config stay git-ignored)**

```bash
cd ~/projects/petlibro && git add README.md && \
  git commit -m "docs: README with setup, calibration, and Claude registration"
```

---

## Calibration & follow-ups (post-build, tracked here — not stub tickets)

- **`portions_per_cup`:** default 12 is a guess. After Step 3 confirms feeding works, dispense a known portion count into a measuring cup, compute the true ratio, set per-feeder overrides in `pets.toml` if food differs by station.
- **Fountain status keys:** `fountain_status` returns the raw `realInfo` for now. Once the smoke test shows a fountain's real payload, add named fields (water level, pump, filter life) reading the actual keys.
- **Deferred (explicitly, not stubbed):** schedule editing, fountain pump control, RFID visit history (`recent_visits`).
