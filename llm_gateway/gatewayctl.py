#!/usr/bin/env python3
"""Manage local LiteLLM gateways: one ``.env.<name>`` == one gateway process.

The gateway name is the only configuration identity. Everything else derives
from it (see ``Gateway``):

    pid       run/gateways/<name>.pid
    logs      logs/gateways/<name>/llm_calls.jsonl   (unless LLM_GATEWAY_LOG_DIR is set)
    registry  run/attribution/<name>.json            (lanes only; LANE_REGISTRY_DIR overrides dir)
    ready     run/attribution/gateway_ready.port-<port>.json

A gateway becomes an attribution *lane* by setting ``LANE_POOL=<group>`` in its
env file; ``LANE_TARGET_URL`` names the business instance bound to it (used by
runners, not by the gateway). Stdlib only: runs from any python3.

Usage (normally via the repo-root ``llmtrace`` script):
    llmtrace gateway list | show NAME | up NAME.. | down NAME.. | restart NAME.. | check [NAME..] | rotate NAME..
NAME may also be a LANE_POOL value (selects every lane in that pool).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

GATEWAY_DIR = Path(__file__).resolve().parent
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SECRET_RE = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD)", re.I)
REQUIRED_KEYS = (
    "UPSTREAM_API_BASE", "UPSTREAM_API_KEY", "UPSTREAM_PROTOCOL", "UPSTREAM_MODEL",
    "LITELLM_HOST", "LITELLM_PORT", "LITELLM_MASTER_KEY", "LITELLM_CONFIG",
)
# Inherited settings that would silently change the child's identity or lane mode.
_STRIP_PREFIXES = ("LANE_", "LLM_ATTRIBUTION_", "LLM_GATEWAY_")


class GatewayError(RuntimeError):
    pass


def parse_env(path: Path) -> dict[str, str]:
    """KEY=VALUE parser (``export`` prefix, quotes, trailing `` # comment``); no shell expansion."""
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.startswith("export "):
            s = s[len("export "):].strip()
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip()
        if v[:1] in ("'", '"') and v[:1] in v[1:]:
            v = v[1:v.index(v[0], 1)]
        else:
            v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
        if k:
            out[k] = v
    return out


def mask(key: str, value: str) -> str:
    if not SECRET_RE.search(key) or not value:
        return value
    return value[:3] + "***" if len(value) > 8 else "***"


@dataclass
class Gateway:
    name: str
    env_file: Path
    env: dict[str, str]
    gateway_dir: Path = field(default=GATEWAY_DIR)

    def _path(self, value: str) -> Path:
        p = Path(value).expanduser()
        return (p if p.is_absolute() else self.gateway_dir / p).resolve()

    @property
    def host(self) -> str:
        return self.env.get("LITELLM_HOST", "127.0.0.1")

    @property
    def port(self) -> str:
        return self.env.get("LITELLM_PORT", "")

    @property
    def master_key(self) -> str:
        return self.env.get("LITELLM_MASTER_KEY", "")

    @property
    def lane_pool(self) -> Optional[str]:
        return self.env.get("LANE_POOL") or None

    @property
    def is_lane(self) -> bool:
        return bool(self.lane_pool)

    @property
    def target_url(self) -> Optional[str]:
        return self.env.get("LANE_TARGET_URL") or None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def litellm_config(self) -> Optional[Path]:
        raw = self.env.get("LITELLM_CONFIG")
        return self._path(raw) if raw else None

    @property
    def log_dir(self) -> Path:
        raw = self.env.get("LLM_GATEWAY_LOG_DIR")
        return self._path(raw) if raw else (self.gateway_dir / "logs" / "gateways" / self.name)

    @property
    def log_file(self) -> Path:
        return self.log_dir / "llm_calls.jsonl"

    @property
    def stdout_log(self) -> Path:
        return self.log_dir / "litellm.stdout.log"

    @property
    def run_dir(self) -> Path:
        return self.gateway_dir / "run" / "gateways"

    @property
    def pid_file(self) -> Path:
        return self.run_dir / f"{self.name}.pid"

    @property
    def state_file(self) -> Path:
        return self.run_dir / f"{self.name}.state.json"

    @property
    def registry_dir(self) -> Path:
        raw = self.env.get("LANE_REGISTRY_DIR")
        return self._path(raw) if raw else (self.gateway_dir / "run" / "attribution").resolve()

    @property
    def ready_marker(self) -> Path:
        return self.registry_dir / f"gateway_ready.port-{self.port}.json"

    def fingerprint(self) -> str:
        h = hashlib.sha256(self.env_file.read_bytes())
        cfg = self.litellm_config
        if cfg and cfg.is_file():
            h.update(cfg.read_bytes())
        return h.hexdigest()

    def env_sha256(self) -> str:
        return hashlib.sha256(self.env_file.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- discovery
def env_file_for(name: str, gateway_dir: Path = GATEWAY_DIR) -> Path:
    return Path(gateway_dir) / (".env" if name == "default" else f".env.{name}")


def _name_from_file(path: Path) -> Optional[str]:
    if path.name == ".env":
        return "default"
    name = path.name[len(".env."):]
    if name == "example" or not NAME_RE.fullmatch(name) or name.startswith("bak"):
        return None
    return name


def load(name: str, gateway_dir: Path = GATEWAY_DIR) -> Gateway:
    if not NAME_RE.fullmatch(name or ""):
        raise GatewayError(f"invalid gateway name: {name!r}")
    path = env_file_for(name, gateway_dir)
    if not path.is_file():
        raise GatewayError(f"unknown gateway {name!r}: missing {path}")
    return Gateway(name=name, env_file=path.resolve(), env=parse_env(path), gateway_dir=Path(gateway_dir).resolve())


def discover(gateway_dir: Path = GATEWAY_DIR) -> list[Gateway]:
    out = []
    for path in sorted(Path(gateway_dir).glob(".env*")):
        name = _name_from_file(path) if path.is_file() else None
        if not name:
            continue
        g = load(name, gateway_dir)
        # Other local services (e.g. thin_proxy's .env.thin_*) share the .env.* namespace.
        if any(k.startswith("LITELLM_") for k in g.env) or g.is_lane:
            out.append(g)
    return out


def select(tokens: list[str], gateway_dir: Path = GATEWAY_DIR) -> list[Gateway]:
    """Resolve gateway names or LANE_POOL values (comma or space separated)."""
    names = [t for tok in tokens for t in tok.split(",") if t.strip()]
    if not names:
        raise GatewayError("no gateway selected")
    every = {g.name: g for g in discover(gateway_dir)}
    picked: dict[str, Gateway] = {}
    for token in names:
        if token in every:
            picked[token] = every[token]
            continue
        pool = [g for g in every.values() if g.lane_pool == token]
        if not pool:
            raise GatewayError(f"no gateway or lane pool named {token!r}")
        for g in pool:
            picked[g.name] = g
    return list(picked.values())


def lanes(selector: str, gateway_dir: Path = GATEWAY_DIR) -> list[Gateway]:
    """Gateways usable as runner lanes: every one must be a lane with a target URL."""
    picked = select([selector], gateway_dir)
    for g in picked:
        if not g.is_lane:
            raise GatewayError(f"gateway {g.name} is not a lane (set LANE_POOL in {g.env_file.name})")
        if not g.target_url:
            raise GatewayError(f"lane {g.name} has no LANE_TARGET_URL")
    return picked


# --------------------------------------------------------------------------- checks
def check(gateways: list[Gateway], *, universe: Optional[list[Gateway]] = None) -> tuple[list[str], list[str]]:
    """Static config validation. Conflicts are errors when a lane is involved."""
    errors: list[str] = []
    warnings: list[str] = []
    for g in gateways:
        missing = [k for k in REQUIRED_KEYS if not g.env.get(k)]
        if missing:
            errors.append(f"{g.name}: missing {', '.join(missing)}")
        if g.port and not g.port.isdigit():
            errors.append(f"{g.name}: LITELLM_PORT must be a number")
        cfg = g.litellm_config
        if cfg and not cfg.is_file():
            errors.append(f"{g.name}: LITELLM_CONFIG not found: {cfg}")
        if g.is_lane:
            if not NAME_RE.fullmatch(g.lane_pool or ""):
                errors.append(f"{g.name}: invalid LANE_POOL")
            if not g.target_url:
                warnings.append(f"{g.name}: lane without LANE_TARGET_URL (runner cannot use it)")
            elif not g.target_url.startswith(("http://", "https://")):
                errors.append(f"{g.name}: LANE_TARGET_URL must be http(s)")
            if g.env.get("LLM_ATTRIBUTION_CONFIG"):
                warnings.append(f"{g.name}: LLM_ATTRIBUTION_CONFIG set; legacy json mode wins over LANE_POOL")
        elif g.target_url:
            warnings.append(f"{g.name}: LANE_TARGET_URL ignored without LANE_POOL")
    pool = universe if universe is not None else gateways
    selected = {g.name for g in gateways}

    def conflicts(key, label: str, *, lane_only_error: bool = True) -> None:
        seen: dict[Any, list[Gateway]] = {}
        for g in pool:
            value = key(g)
            if value:
                seen.setdefault(value, []).append(g)
        for group in seen.values():
            if len(group) < 2 or not any(g.name in selected for g in group):
                continue
            names = ", ".join(g.name for g in group)
            if any(g.is_lane for g in group) or not lane_only_error:
                errors.append(f"{label} shared by {names}")
            else:
                warnings.append(f"{label} shared by {names} (alternative profiles; run one at a time)")

    conflicts(lambda g: g.port, "port")
    conflicts(lambda g: str(g.log_dir), "log dir")
    conflicts(lambda g: g.target_url if g.is_lane else None, "LANE_TARGET_URL")
    keys: dict[str, list[str]] = {}
    for g in pool:
        if g.is_lane and g.master_key:
            keys.setdefault(g.master_key, []).append(g.name)
    for names in keys.values():
        if len(names) > 1 and selected & set(names):
            warnings.append(f"LITELLM_MASTER_KEY shared by lanes {', '.join(names)} "
                            "(safe per process, but a client key cannot tell them apart)")
    return errors, warnings


# --------------------------------------------------------------------------- runtime state
def _pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        # Reap our own exited child; a zombie still answers kill(pid, 0).
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_pid(g: Gateway) -> Optional[int]:
    try:
        return int(g.pid_file.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def healthy(g: Gateway, timeout: float = 2.0) -> bool:
    if not g.port:
        return False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(f"{g.base_url}/v1/models", headers={"Authorization": f"Bearer {g.master_key}"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200) == 200
    except Exception:  # noqa: BLE001 - any failure means not healthy
        return False


def lane_ready(g: Gateway, pid: Optional[int]) -> bool:
    """The ready marker was written by *this* process from *this* env file."""
    if not g.is_lane:
        return True
    marker = _read_json(g.ready_marker)
    return (
        pid is not None and marker.get("pid") == pid
        and marker.get("mode") == "env" and marker.get("gateway") == g.name
        and marker.get("config_path") == str(g.env_file)
        and marker.get("config_sha256") == g.env_sha256()
    )


def port_owner(g: Gateway, universe: Optional[list[Gateway]] = None) -> Optional[Gateway]:
    """Another llmtrace gateway currently running on g's port (alternative profiles share 4001)."""
    for other in universe if universe is not None else discover(g.gateway_dir):
        if other.name != g.name and other.port == g.port and _pid_alive(read_pid(other)):
            return other
    return None


def status(g: Gateway, *, timeout: float = 2.0, universe: Optional[list[Gateway]] = None) -> dict[str, Any]:
    pid = read_pid(g)
    running = _pid_alive(pid)
    owner = None if running else port_owner(g, universe)
    ok = healthy(g, timeout=timeout) if owner is None else False
    state = _read_json(g.state_file)
    return {
        "name": g.name, "port": g.port, "pid": pid if running else None,
        "running": running, "healthy": ok,
        # Answering on the port with this key, but not started by llmtrace.
        "external": ok and not running,
        "port_owner": owner.name if owner else None,
        "stale_config": running and state.get("fingerprint") not in (None, g.fingerprint()),
        "lane_pool": g.lane_pool, "target_url": g.target_url,
        "ready": (lane_ready(g, pid if running else None) if g.is_lane else None),
        "log_file": str(g.log_file),
    }


def _state_label(st: dict[str, Any]) -> str:
    if st["running"]:
        label = "up" if st["healthy"] else "starting/unhealthy"
        if st["ready"] is False:
            label += ",lane-not-ready"
        if st["stale_config"]:
            label += ",config-changed"
        return label
    if st.get("port_owner"):
        return f"down(:{st['port']} used by {st['port_owner']})"
    return "up(external)" if st["external"] else "down"


def port_listeners(port: str) -> list[int]:
    if not port or not shutil.which("lsof"):
        return []
    proc = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                          capture_output=True, text=True, check=False)
    return [int(x) for x in proc.stdout.split() if x.strip().isdigit()]


def child_env(g: Gateway, base: Optional[dict[str, str]] = None) -> dict[str, str]:
    base = dict(os.environ if base is None else base)
    env = {k: v for k, v in base.items() if not k.startswith(_STRIP_PREFIXES)}
    env.update(g.env)
    # Keep truncated streams observable; allow an explicit profile override.
    env.setdefault("DISABLE_AIOHTTP_TRANSPORT", "True")
    env["UPSTREAM_LITELLM_MODEL"] = f"{g.env.get('UPSTREAM_PROTOCOL', '')}/{g.env.get('UPSTREAM_MODEL', '')}"
    env["ANTHROPIC_API_KEY"] = g.env.get("UPSTREAM_API_KEY", "")
    upstream_host = re.sub(r"^[a-z]+://([^/:]+).*$", r"\1", g.env.get("UPSTREAM_API_BASE", ""))
    direct = g.env.get("GATEWAY_DIRECT_UPSTREAM", base.get("GATEWAY_DIRECT_UPSTREAM", "auto"))
    if direct == "1" or (direct == "auto" and upstream_host.endswith(".aliyuncs.com")):
        current = base.get("NO_PROXY", "")
        env["NO_PROXY"] = "127.0.0.1,localhost,::1,.aliyuncs.com" + (f",{current}" if current else "")
        env["no_proxy"] = env["NO_PROXY"]
    cfg = g.litellm_config
    if cfg:
        env["LITELLM_CONFIG"] = str(cfg)
    env.update({
        "LLM_GATEWAY_NAME": g.name,
        "LLM_GATEWAY_PROFILE": g.name,
        "LLM_GATEWAY_ENV_FILE": str(g.env_file),
        "LLM_GATEWAY_LOG_DIR": str(g.log_dir),
        "LLM_ATTRIBUTION_GATEWAY_PROCESS": "1",
        # Profiles/runtime may belong to the consuming project; callbacks remain
        # in this installation. Do not require copying them into the profile dir.
        "PYTHONPATH": os.pathsep.join(dict.fromkeys([str(GATEWAY_DIR), str(g.gateway_dir)]))
                      + (os.pathsep + base["PYTHONPATH"] if base.get("PYTHONPATH") else ""),
    })
    if g.is_lane:
        # Absolute, so the callback and the runner agree on the registry location.
        env["LANE_REGISTRY_DIR"] = str(g.registry_dir)
    env.setdefault("LITELLM_LOG", "INFO")
    return env


def litellm_bin(g: Gateway) -> str:
    for directory in dict.fromkeys([g.gateway_dir, GATEWAY_DIR]):
        local = directory / ".venv" / "bin" / "litellm"
        if local.is_file():
            return str(local)
    found = shutil.which("litellm")
    if not found:
        raise GatewayError("litellm not found (expected llm_gateway/.venv/bin/litellm)")
    return found


def _kill_tree(pid: int, sig: int) -> None:
    for target in (lambda: os.killpg(pid, sig), lambda: os.kill(pid, sig)):
        try:
            target()
            return
        except (ProcessLookupError, PermissionError, OSError):
            continue


def _wait_dead(pids: list[int], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_pid_alive(p) for p in pids):
            return True
        time.sleep(0.2)
    return not any(_pid_alive(p) for p in pids)


def down(g: Gateway, *, force: bool = False, out=sys.stdout) -> None:
    pid = read_pid(g)
    if _pid_alive(pid):
        _kill_tree(pid, signal.SIGTERM)
        if not _wait_dead([pid], 10.0):
            _kill_tree(pid, signal.SIGKILL)
            _wait_dead([pid], 3.0)
        print(f"{g.name}: stopped pid={pid}", file=out)
    g.pid_file.unlink(missing_ok=True)
    if force:
        others = port_listeners(g.port)
        for p in others:
            _kill_tree(p, signal.SIGTERM)
        if others and not _wait_dead(others, 5.0):
            raise GatewayError(f"{g.name}: listeners still on :{g.port}: {others}")
        if others:
            print(f"{g.name}: stopped external listeners on :{g.port}: {others}", file=out)


def up(g: Gateway, *, restart: bool = False, force: bool = False, wait: float = 40.0,
       universe: Optional[list[Gateway]] = None, out=sys.stdout) -> dict[str, Any]:
    universe = universe if universe is not None else discover(g.gateway_dir)
    errors, warnings = check([g], universe=universe)
    for w in warnings:
        print(f"warning: {w}", file=out)
    if errors:
        raise GatewayError("; ".join(errors))
    st = status(g, universe=universe)
    if st["running"] and st["healthy"] and st["ready"] is not False and not st["stale_config"] and not restart:
        print(f"{g.name}: already up pid={st['pid']} {g.base_url}", file=out)
        _record_primary(g)
        return st
    if st["running"]:
        down(g, out=out)
    sibling = port_owner(g, universe)
    if sibling is not None:
        # Switching between alternative profiles of one port is an explicit choice.
        if not force:
            raise GatewayError(f"{g.name}: :{g.port} is used by gateway {sibling.name}; "
                               f"run `llmtrace gateway down {sibling.name}` first or pass --force to switch")
        down(sibling, out=out)
    foreign = [p for p in port_listeners(g.port) if p != read_pid(g)]
    if foreign:
        if not force:
            raise GatewayError(f"{g.name}: :{g.port} is held by pid {foreign} (not started by llmtrace); "
                               "stop it or pass --force")
        down(g, force=True, out=out)
    g.log_dir.mkdir(parents=True, exist_ok=True)
    g.run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [litellm_bin(g), "--config", str(g.litellm_config), "--host", g.host, "--port", g.port,
           "--telemetry", "False"]
    with g.stdout_log.open("ab") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=child_env(g), cwd=str(g.gateway_dir), start_new_session=True)
    g.pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
    g.state_file.write_text(json.dumps({"pid": proc.pid, "fingerprint": g.fingerprint(),
                                        "env_file": str(g.env_file), "started_at_epoch": time.time()}) + "\n",
                            encoding="utf-8")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            g.pid_file.unlink(missing_ok=True)
            raise GatewayError(f"{g.name}: litellm exited early code={proc.returncode}; see {g.stdout_log}")
        if healthy(g, timeout=1.5) and lane_ready(g, proc.pid):
            print(f"{g.name}: up pid={proc.pid} {g.base_url} log={g.log_file}"
                  + (f" lane_pool={g.lane_pool}" if g.is_lane else ""), file=out)
            _record_primary(g)
            return status(g, universe=universe)
        time.sleep(0.5)
    raise GatewayError(f"{g.name}: started pid={proc.pid} but not healthy/ready within {wait:.0f}s; see {g.stdout_log}")


# --------------------------------------------------------------------------- log rotation
def _record_primary(g: Gateway) -> None:
    """Keep run/active_* current for the primary relay port (the default ``.env``'s).

    eval_harness.suite reads run/active_env_file to find the live profile's key/port.
    """
    default = env_file_for("default", g.gateway_dir)
    if not default.is_file() or parse_env(default).get("LITELLM_PORT") != g.port:
        return
    run = g.gateway_dir / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "active_profile").write_text(f"{g.name}\n", encoding="utf-8")
    (run / "active_env_file").write_text(f"{g.env_file}\n", encoding="utf-8")
    (run / "active_env_sha256").write_text(f"{g.env_sha256()}\n", encoding="utf-8")


def lane_busy(g: Gateway) -> Optional[str]:
    """Why the lane's log must not move now (a runner holds its lease), else None."""
    if not g.is_lane:
        return None
    import fcntl
    lock_path = g.registry_dir / f"{g.name}.lock"
    if lock_path.exists():
        with lock_path.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
            except OSError:
                return "lane is leased by a running eval"
    state = _read_json(g.registry_dir / f"{g.name}.json").get("state")
    if state not in (None, "idle"):
        return f"lane state is {state!r}"
    return None


def rotated_logs(g: Gateway) -> list[Path]:
    return sorted(g.log_dir.glob("llm_calls_*.jsonl.gz"))


def rotate(g: Gateway, *, keep: Optional[int] = None, force: bool = False, out=sys.stdout) -> Optional[Path]:
    """Move llm_calls.jsonl to llm_calls_<UTC stamp>.jsonl.gz; the gateway starts a new file.

    Safe while the gateway runs: the callback reopens the file per record, so
    after the rename new records go to a fresh file. Refused while a runner
    holds the lane (its read offsets point into the current file).
    """
    import gzip
    reason = lane_busy(g)
    if reason and not force:
        raise GatewayError(f"{g.name}: not rotating, {reason} (use --force to override)")
    archive = None
    if g.log_file.is_file() and g.log_file.stat().st_size > 0:
        stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        staged = g.log_dir / f".llm_calls_{stamp}.jsonl.rotating"
        archive = g.log_dir / f"llm_calls_{stamp}.jsonl.gz"
        if archive.exists():
            raise GatewayError(f"{archive} already exists; retry in a second")
        os.replace(g.log_file, staged)
        time.sleep(0.2)  # let an append that opened the old inode finish
        partial = archive.with_name(archive.name + ".part")
        with staged.open("rb") as src, gzip.open(partial, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        os.replace(partial, archive)
        staged.unlink()
        print(f"{g.name}: rotated -> {archive} ({archive.stat().st_size} bytes)", file=out)
    else:
        print(f"{g.name}: nothing to rotate", file=out)
    if keep is not None:
        archives = rotated_logs(g)
        for old in archives[:max(len(archives) - max(keep, 0), 0)]:
            old.unlink()
            print(f"{g.name}: removed {old.name}", file=out)
    return archive


# --------------------------------------------------------------------------- CLI
def _print_table(rows: list[list[str]], out=sys.stdout) -> None:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip(), file=out)


def _rel(path: Path, root: Path) -> str:
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


def cmd_list(args, gateway_dir: Path) -> int:
    gws = discover(gateway_dir)
    rows = [status(g, timeout=0.8, universe=gws) for g in gws]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    table = [["NAME", "PORT", "LANE_POOL", "TARGET", "STATUS", "LOG"]]
    for g, st in zip(gws, rows):
        table.append([g.name, g.port or "-", g.lane_pool or "-", g.target_url or "-", _state_label(st),
                      _rel(g.log_file, gateway_dir)])
    _print_table(table)
    return 0


def cmd_show(args, gateway_dir: Path) -> int:
    g = load(args.name, gateway_dir)
    st = status(g)
    info = {
        "name": g.name, "env_file": str(g.env_file), "base_url": g.base_url,
        "lane": ({"pool": g.lane_pool, "target_url": g.target_url, "registry": str(g.registry_dir / f"{g.name}.json"),
                  "ready_marker": str(g.ready_marker)} if g.is_lane else None),
        "log_file": str(g.log_file), "stdout_log": str(g.stdout_log), "pid_file": str(g.pid_file),
        "status": _state_label(st), "pid": st["pid"],
        "env": {k: (v if args.reveal else mask(k, v)) for k, v in g.env.items()},
    }
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    for k, v in info.items():
        if k == "env":
            print("env:")
            for ek, ev in v.items():
                print(f"  {ek}={ev}")
        elif isinstance(v, dict):
            print(f"{k}:")
            for sk, sv in v.items():
                print(f"  {sk}: {sv}")
        else:
            print(f"{k}: {v if v is not None else '-'}")
    return 0


def cmd_check(args, gateway_dir: Path) -> int:
    universe = discover(gateway_dir)
    gws = select(args.names, gateway_dir) if args.names else universe
    errors, warnings = check(gws, universe=universe)
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    print(f"checked {len(gws)} gateway(s): {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


def cmd_up(args, gateway_dir: Path, *, restart: bool = False) -> int:
    universe = discover(gateway_dir)
    rc = 0
    for g in select(args.names, gateway_dir):
        try:
            up(g, restart=restart or getattr(args, "restart", False), force=args.force,
               wait=args.wait, universe=universe)
        except GatewayError as exc:
            print(f"error: {exc}", file=sys.stderr)
            rc = 1
    return rc


def cmd_down(args, gateway_dir: Path) -> int:
    rc = 0
    for g in select(args.names, gateway_dir):
        try:
            down(g, force=args.force)
        except GatewayError as exc:
            print(f"error: {exc}", file=sys.stderr)
            rc = 1
    return rc


def cmd_rotate(args, gateway_dir: Path) -> int:
    rc = 0
    for g in select(args.names, gateway_dir):
        try:
            rotate(g, keep=args.keep, force=args.force)
        except GatewayError as exc:
            print(f"error: {exc}", file=sys.stderr)
            rc = 1
    return rc


def build_parser(prog: str = "llmtrace gateway") -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=prog, description="Manage local LiteLLM gateways (.env.<name>).")
    ap.add_argument("--gateway-dir", type=Path, default=None, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("list", help="all gateways with port, lane pool, status, log path")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="one gateway's derived paths, status and (masked) env")
    p.add_argument("name")
    p.add_argument("--reveal", action="store_true", help="print secret values unmasked")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("check", help="validate env files and cross-gateway conflicts")
    p.add_argument("names", nargs="*")
    for action in ("up", "restart"):
        p = sub.add_parser(action, help=f"{action} gateway(s) by name or lane pool")
        p.add_argument("names", nargs="+")
        p.add_argument("--force", action="store_true", help="also stop a foreign listener on the port")
        p.add_argument("--wait", type=float, default=40.0, help="seconds to wait for health/ready")
        if action == "up":
            p.add_argument("--restart", action="store_true")
    p = sub.add_parser("down", help="stop gateway(s) by name or lane pool")
    p.add_argument("names", nargs="+")
    p.add_argument("--force", action="store_true", help="also stop a foreign listener on the port")
    p = sub.add_parser("rotate", help="gzip the current call log aside (safe while running; not while leased)")
    p.add_argument("names", nargs="+")
    p.add_argument("--keep", type=int, default=None, help="keep only the newest N rotated archives")
    p.add_argument("--force", action="store_true", help="rotate even if the lane is leased")
    return ap


def main(argv: Optional[list[str]] = None, *, prog: str = "llmtrace gateway") -> int:
    args = build_parser(prog).parse_args(argv)
    gateway_dir = Path(args.gateway_dir or os.environ.get("LLMTRACE_GATEWAY_DIR") or GATEWAY_DIR).resolve()
    try:
        if args.action == "list":
            return cmd_list(args, gateway_dir)
        if args.action == "show":
            return cmd_show(args, gateway_dir)
        if args.action == "check":
            return cmd_check(args, gateway_dir)
        if args.action == "up":
            return cmd_up(args, gateway_dir)
        if args.action == "restart":
            return cmd_up(args, gateway_dir, restart=True)
        if args.action == "down":
            return cmd_down(args, gateway_dir)
        if args.action == "rotate":
            return cmd_rotate(args, gateway_dir)
    except GatewayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
