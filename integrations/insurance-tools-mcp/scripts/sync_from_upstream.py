"""从上游 insurance-qa-agent 同步工具实现层 —— 按符号抽取、分段暂存、验证通过才原子替换。

用法:
    .venv/bin/python scripts/sync_from_upstream.py                # 同步(强制重建+验证+替换)
    .venv/bin/python scripts/sync_from_upstream.py --check        # 只查是否漂移:0 对齐 / 2 漂移
    .venv/bin/python scripts/sync_from_upstream.py --ensure       # 漂移才同步(评测 preflight 用)
    .venv/bin/python scripts/sync_from_upstream.py --validate-only # 只验证当前在用的树
  (任何解释器都可以启动;脚本会自动切到本工程 .venv 的 Python 3.12 执行。)

上游:缺省 = scripts/sync_config.json 的 `source`,即评测 worktree
`insurance-qa-agent-eval`(eval/current)——与 :18063 评测 Insurance 服务同一棵树,
也与 16:17 那次旧同步读的树一致。`--source` 可覆盖(insurance_upstream_align.py 显式传入)。
上游两棵树只读,本脚本从不写它们。

同步面(与旧版相同):
  1. 以 7 个工具 + 配置装载 + adapter 工厂 + 后端组合根 + S1 会话态为种子,AST 求运行期 import 闭包,
     逐字复制闭包内文件(外加所在包的 __init__.py);
  2. 闭包里依赖 agno 的模块(app.runtime.model_router / model_stream)不整文件复制;
     闭包里别的模块从它们 import 的**符号**被自动收集(目前是 HarnessConf),
     由 symbol_extract.extract_symbols 按符号 + 依赖闭包抽取生成(含 typing/Literal 等 import,
     未解析名字/agno 依赖 → 同步失败);
  3. app/conf/(排除 models/,内含开发期明文凭据)与 capabilities/ 整体复制;
  4. 用 MCP .venv(含 agno)从上游 bindings 导出 itools/tools_spec.json。

流程:全部生成到 <root>/.sync_staging_*;scripts/sync_validate.py 在候选树上做
py_compile → import → harness_conf() → stdio MCP tools/list + 真实只读 tools/call → spec 比对;
全部通过才把 app/ tool_adapter_sdk/ capabilities/ itools/tools_spec.json 逐个 rename 换入
(同文件系统原子 rename,任一步失败整体回滚),旧版本移入 .sync_backups/<ts>/(保留最近 3 份)。
换入后对在用树再验证一次,不过则自动回滚。验证失败时在用文件一字不动,退出码非 0。
结果与来源记录在 <root>/SYNC_STATE.json。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
DEFAULT_ROOT = SCRIPTS.parent
VENV_PY = DEFAULT_ROOT / ".venv" / "bin" / "python"
CONFIG_PATH = SCRIPTS / "sync_config.json"
STATE_NAME = "SYNC_STATE.json"
LEGACY_STAMP = Path("itools") / "upstream_sync_stamp.json"
BACKUP_DIR = ".sync_backups"
STAGING_PREFIX = ".sync_staging_"
KEEP_BACKUPS = 3

ROOTS = {"app", "tool_adapter_sdk"}
SEEDS = [
    "app.tools.product_knowledge", "app.tools.benefit_info", "app.tools.general_qa",
    "app.tools.policy_search", "app.tools.customer_search", "app.tools.policy_detail",
    "app.tools.customer_detail", "app.tools.real.factory", "app.conf.tool_config_loader",
    # 后端组合根:上游 /v1/chat 走的就是这一条(ManagedBackendClient + 配置代)。
    "app.runtime.backend_provider",
    # 上游每轮 run 的会话态初始化(S1):五命名空间 + 单轮预算账本。
    "app.orchestration.assembler",
    "app.core.budget",
]
# 这些模块整体依赖 agno;工具层只需要其中的纯 conf 契约 → 按符号抽取,禁止带入 agno。
SYMBOL_EXTRACT_MODULES = {"app.runtime.model_router", "app.runtime.model_stream"}
FORBIDDEN_IN_EXTRACT = ("agno",)
SKIP_CONF_PARTS = {("app", "conf", "models")}
SKIP_NAMES = {".DS_Store"}
MANAGED_DIRS = ("app", "tool_adapter_sdk", "capabilities")
MANAGED_FILES = ("itools/tools_spec.json",)
ITOOLS_LOCAL_MODULES = ("itools", "itools.app", "itools.dispatch", "itools.identity",
                        "itools.runtime", "itools.spec", "itools.__main__")


class SyncError(RuntimeError):
    pass


# --------------------------------------------------------------------------- utils
def _now() -> datetime:
    return datetime.now().astimezone()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _noise(path: Path) -> bool:
    return path.name in SKIP_NAMES or path.suffix in {".pyc", ".pyo"} or "__pycache__" in path.parts


def load_config() -> dict:
    if CONFIG_PATH.is_file():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def reexec_in_venv(argv: list[str]) -> None:
    """旧入口(umbrella)用系统 python3.9 调本脚本;抽取/验证需要与 MCP 同一解释器(3.12)。"""
    if os.environ.get("ITOOLS_SYNC_REEXEC") == "1" or sys.version_info[:2] >= (3, 12):
        return
    if not VENV_PY.is_file():
        raise SystemExit(f"需要 Python>=3.12;找不到 {VENV_PY}")
    env = dict(os.environ, ITOOLS_SYNC_REEXEC="1")
    os.execve(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve()), *argv], env)


def upstream_identity(source: Path) -> dict:
    out = {"path": str(source), "commit": None, "short": None, "branch": None, "dirty": None, "error": None}
    try:
        def git(*args: str) -> str:
            return subprocess.check_output(["git", "-C", str(source), *args], text=True,
                                           stderr=subprocess.DEVNULL).strip()
        out["commit"] = git("rev-parse", "HEAD")
        out["short"] = out["commit"][:12]
        out["branch"] = git("branch", "--show-current") or None
        out["dirty"] = bool(git("status", "--porcelain", "--untracked-files=no"))
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        out["error"] = str(exc)
    return out


# --------------------------------------------------------------------------- closure
def _exists_exact(path: Path) -> bool:
    """大小写精确匹配(macOS 默认大小写不敏感:`app/schemas/Evidence` 会误中 evidence.py)。"""
    if not path.exists():
        return False
    try:
        return path.name in os.listdir(path.parent)
    except OSError:
        return False


def module_file(source: Path, module: str) -> Path | None:
    base = source / Path(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file() and _exists_exact(candidate) and _exists_exact(candidate.parent):
            return candidate
    return None


def _package_parts(source: Path, module: str) -> list[str]:
    parts = module.split(".")
    return parts if (source / Path(*parts)).is_dir() else parts[:-1]


def _resolve_from(source: Path, module: str | None, level: int, current: str) -> str | None:
    if level == 0:
        return module
    base = _package_parts(source, current)
    base = base[: len(base) - (level - 1)] if level > 1 else base
    return ".".join([*base, module]) if module else ".".join(base)


def runtime_import_nodes(tree: ast.AST) -> list[ast.AST]:
    """运行期 import 节点(含函数体内惰性 import);`if TYPE_CHECKING:` 块跳过。"""
    found: list[ast.AST] = []

    class Visitor(ast.NodeVisitor):
        def visit_If(self, node: ast.If) -> None:
            t = node.test
            if (isinstance(t, ast.Name) and t.id == "TYPE_CHECKING") or (
                    isinstance(t, ast.Attribute) and t.attr == "TYPE_CHECKING"):
                for child in node.orelse:
                    self.visit(child)
                return
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            found.append(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            found.append(node)

    Visitor().visit(tree)
    return found


def import_closure(source: Path) -> tuple[list[str], dict[str, set[str]]]:
    """返回 (闭包模块, {按符号抽取模块: 被其它闭包模块 import 的符号集合})。

    按符号抽取的模块不整文件沿 import 扩闭包;但抽取结果里保留的 import(例如 HarnessConf
    字段类型来自 `app.core.context_overflow`)同样是运行期依赖,必须纳入闭包,否则候选树缺模块。
    抽取出的 import 可能又点名更多符号,故迭代到不动点。
    """
    from symbol_extract import ExtractionError, extract_symbols  # noqa: PLC0415

    seen: dict[str, Path] = {}
    wanted: dict[str, set[str]] = {m: set() for m in SYMBOL_EXTRACT_MODULES}
    problems: list[str] = []
    queue = list(SEEDS)

    def follow(module: str, nodes: list[ast.AST]) -> None:
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in SYMBOL_EXTRACT_MODULES:
                        problems.append(f"{module}: `import {alias.name}` 属性式访问无法按符号抽取")
                    elif alias.name.split(".")[0] in ROOTS:
                        queue.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                target = _resolve_from(source, node.module, node.level, module)
                if not target or target.split(".")[0] not in ROOTS:
                    continue
                if target in SYMBOL_EXTRACT_MODULES:
                    wanted[target] |= {a.name for a in node.names}
                    queue.append(target)
                    continue
                if module_file(source, target):
                    queue.append(target)
                for alias in node.names:
                    sub = f"{target}.{alias.name}"
                    if sub in SYMBOL_EXTRACT_MODULES:
                        problems.append(f"{module}: `from {target} import {alias.name}` 模块式访问无法按符号抽取")
                    elif module_file(source, sub):
                        queue.append(sub)

    extracted_for: dict[str, frozenset[str]] = {}
    while True:
        while queue:
            module = queue.pop()
            if module in seen:
                continue
            path = module_file(source, module)
            if path is None:
                continue
            seen[module] = path
            if module in SYMBOL_EXTRACT_MODULES:
                continue  # 不沿 agno 模块整文件扩闭包;它只贡献被点名的符号(及其 import,见下)
            follow(module, runtime_import_nodes(ast.parse(path.read_text(encoding="utf-8"))))
        changed = False
        for mod, symbols in sorted(wanted.items()):
            if not symbols or extracted_for.get(mod) == frozenset(symbols) or mod not in seen:
                continue
            extracted_for[mod] = frozenset(symbols)
            changed = True
            try:
                ext = extract_symbols(seen[mod].read_text(encoding="utf-8"), sorted(symbols),
                                      filename=mod, forbidden_modules=FORBIDDEN_IN_EXTRACT)
            except ExtractionError as exc:
                raise SyncError(str(exc)) from exc
            follow(mod, [n for line in ext.imports for n in ast.parse(line).body])
        if not queue and not changed:
            break
    if problems:
        raise SyncError("闭包分析失败:\n  - " + "\n  - ".join(problems))
    return sorted(seen), {k: v for k, v in wanted.items() if v}


def _skip_conf(rel: Path) -> bool:
    return tuple(rel.parts[:3]) in SKIP_CONF_PARTS


def upstream_inputs(source: Path, closure: list[str]) -> dict[str, Path]:
    """同步会读取的全部上游文件(rel → path):闭包 + 包 __init__ + conf + capabilities。"""
    files: dict[str, Path] = {}
    for module in closure:
        path = module_file(source, module)
        if path is None:
            continue
        rel = path.relative_to(source)
        if _skip_conf(rel):
            continue
        files[rel.as_posix()] = path
        # 所在包链上的 __init__.py(上游存在才带)
        for parent in rel.parents:
            if parent == Path("."):
                break
            init = source / parent / "__init__.py"
            if init.is_file():
                files[(parent / "__init__.py").as_posix()] = init
    for sub in (source / "app" / "conf", source / "capabilities"):
        for path in sub.rglob("*"):
            if not path.is_file() or _noise(path):
                continue
            rel = path.relative_to(source)
            if _skip_conf(rel):
                continue
            files[rel.as_posix()] = path
    return files


def sync_logic_digest() -> str:
    h = hashlib.sha256()
    for name in ("sync_from_upstream.py", "symbol_extract.py", "sync_validate.py", "gen_tools_spec.py"):
        p = SCRIPTS / name
        h.update(name.encode() + b"\0" + (p.read_bytes() if p.is_file() else b"") + b"\0")
    return h.hexdigest()


def inputs_digest(files: dict[str, Path]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode() + b"\0" + _sha256(files[rel]).encode() + b"\n")
    return h.hexdigest()


def managed_files(root: Path) -> dict[str, str]:
    """在用树里受同步管理的文件 → sha256。"""
    out: dict[str, str] = {}
    for d in MANAGED_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and not _noise(p):
                out[p.relative_to(root).as_posix()] = _sha256(p)
    for f in MANAGED_FILES:
        p = root / f
        if p.is_file():
            out[f] = _sha256(p)
    return out


# --------------------------------------------------------------------------- build
def build_staging(source: Path, root: Path, python: str, identity: dict) -> dict:
    from symbol_extract import ExtractionError, extract_symbols  # noqa: PLC0415

    closure, wanted = import_closure(source)
    inputs = upstream_inputs(source, closure)
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=str(root)))
    info: dict = {"staging": str(staging), "closure_modules": len(closure), "extracted": {}}
    try:
        copied = 0
        for rel, src in sorted(inputs.items()):
            module_rel = Path(rel)
            mod = ".".join(module_rel.with_suffix("").parts)
            if mod in SYMBOL_EXTRACT_MODULES:
                continue
            dst = staging / module_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        info["files_copied"] = copied
        for mod, symbols in sorted(wanted.items()):
            src = module_file(source, mod)
            assert src is not None
            rel = src.relative_to(source).as_posix()
            header = (
                f'"""按符号自动抽取 —— 请勿手改(下次同步会覆盖)。\n\n'
                f"来源: 上游 {rel} @ {identity.get('short') or 'unknown'}"
                f"{' (dirty)' if identity.get('dirty') else ''}\n"
                f"根符号(闭包内模块 import 的名字): {', '.join(sorted(symbols))}\n"
                "由 scripts/sync_from_upstream.py + scripts/symbol_extract.py 生成:依赖定义与所需 import\n"
                "按 AST/符号表自动求闭包,原文复制;模块其余部分依赖 agno,工具层不需要。\n"
                '"""'
            )
            try:
                ext = extract_symbols(src.read_text(encoding="utf-8"), sorted(symbols), filename=rel,
                                      header=header, forbidden_modules=FORBIDDEN_IN_EXTRACT)
            except ExtractionError as exc:
                raise SyncError(str(exc)) from exc
            dst = staging / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(ext.text, encoding="utf-8")
            info["extracted"][rel] = {"roots": ext.roots, "symbols": ext.symbols, "imports": ext.imports,
                                      "trailing_statements": ext.trailing_statements}
        # itools:本地代码原样带入候选树(不同步),只换 tools_spec.json
        (staging / "itools").mkdir(parents=True, exist_ok=True)
        for p in (root / "itools").glob("*.py"):
            shutil.copy2(p, staging / "itools" / p.name)
        spec_out = staging / "itools" / "tools_spec.json"
        r = subprocess.run([python, str(SCRIPTS / "gen_tools_spec.py"), "--source", str(source),
                            "--out", str(spec_out)], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise SyncError(f"导出 tools_spec.json 失败 rc={r.returncode}: {(r.stderr or r.stdout)[-800:]}")
        info["closure"] = closure
        info["inputs"] = inputs
        return info
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validation_modules(root_or_staging: Path, closure: list[str]) -> list[str]:
    """导入期真实会走到的模块:种子 + 按符号抽取的模块 + itools 自身。

    闭包里还有大量「函数体内惰性 import」才用到的模块(例如 app.core.audit 需要 portalocker),
    MCP 运行期不一定导入它们;是否能用由 d 步真实 tools/call 证明。
    """
    extracted = [m for m in closure if m in SYMBOL_EXTRACT_MODULES and module_file(root_or_staging, m)]
    return list(SEEDS) + extracted + list(ITOOLS_LOCAL_MODULES[:-1])


def run_validation(tree: Path, modules: list[str], cfg: dict) -> dict:
    vcfg = {"modules": modules, "env": cfg.get("validation_env", {}), "probe": cfg["probe"]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(vcfg, fh, ensure_ascii=False)
        cfg_path = fh.name
    try:
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "USER", "LANG", "TMPDIR")}
        env["PYTHONPATH"] = str(tree)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        r = subprocess.run([str(VENV_PY), str(SCRIPTS / "sync_validate.py"), "--root", str(tree),
                            "--modules-file", cfg_path], capture_output=True, text=True, timeout=240,
                           env=env, cwd=str(tree))
    finally:
        os.unlink(cfg_path)
    last = (r.stdout or "").strip().splitlines()[-1:] or [""]
    try:
        res = json.loads(last[0])
    except json.JSONDecodeError:
        res = {"ok": False, "failed_step": "validator_crash",
               "stderr_tail": (r.stderr or "")[-1200:], "stdout_tail": (r.stdout or "")[-400:]}
    res["returncode"] = r.returncode
    res["ok"] = bool(res.get("ok")) and r.returncode == 0
    return res


# --------------------------------------------------------------------------- swap
def _purge_pycache(base: Path) -> None:
    for p in list(base.rglob("__pycache__")):
        shutil.rmtree(p, ignore_errors=True)


def atomic_swap(root: Path, staging: Path, stamp: str) -> Path:
    backup = root / BACKUP_DIR / stamp
    backup.mkdir(parents=True, exist_ok=False)
    (backup / "itools").mkdir()
    done: list[tuple[Path, Path, Path | None]] = []  # (live, staged_src, backup_path)
    try:
        for rel in (*MANAGED_DIRS, *MANAGED_FILES):
            live, new = root / rel, staging / rel
            if not new.exists():
                raise SyncError(f"候选树缺少 {rel}")
            bak = None
            if live.exists():
                bak = backup / rel
                os.rename(live, bak)
            os.rename(new, live)
            done.append((live, new, bak))
    except BaseException:
        for live, new, bak in reversed(done):
            if live.exists():
                os.rename(live, new)
            if bak is not None and bak.exists():
                os.rename(bak, live)
        raise
    legacy = root / LEGACY_STAMP
    if legacy.exists():
        os.rename(legacy, backup / LEGACY_STAMP)
    old_state = root / STATE_NAME
    if old_state.exists():
        shutil.copy2(old_state, backup / STATE_NAME)
    return backup


def rollback(root: Path, backup: Path) -> None:
    trash = Path(tempfile.mkdtemp(prefix=".sync_failed_", dir=str(root)))
    for rel in (*MANAGED_DIRS, *MANAGED_FILES):
        live, bak = root / rel, backup / rel
        if bak.exists():
            if live.exists():
                (trash / rel).parent.mkdir(parents=True, exist_ok=True)
                os.rename(live, trash / rel)
            os.rename(bak, live)
    if (backup / LEGACY_STAMP).exists():
        os.rename(backup / LEGACY_STAMP, root / LEGACY_STAMP)
    shutil.rmtree(trash, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)


def prune_backups(root: Path, keep: int) -> list[str]:
    base = root / BACKUP_DIR
    if not base.is_dir():
        return []
    autos = sorted(p for p in base.iterdir() if p.is_dir() and p.name[:8].isdigit())
    removed = []
    for p in autos[:-keep] if keep > 0 else autos:
        shutil.rmtree(p, ignore_errors=True)
        removed.append(p.name)
    return removed


def write_state(root: Path, state: dict) -> Path:
    path = root / STATE_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------- commands
def check(source: Path, root: Path) -> dict:
    identity = upstream_identity(source)
    closure, _ = import_closure(source)
    digest = inputs_digest(upstream_inputs(source, closure))
    logic = sync_logic_digest()
    state_path = root / STATE_NAME
    reasons: list[str] = []
    state: dict = {}
    if not state_path.is_file():
        reasons.append(f"no {STATE_NAME} (never synced by the validated sync)")
    else:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            reasons.append(f"{STATE_NAME} unreadable")
    if state:
        up = state.get("upstream") or {}
        if Path(up.get("path") or "") != source:
            reasons.append(f"upstream path changed: state={up.get('path')} now={source}")
        if up.get("commit") != identity.get("commit"):
            reasons.append(f"upstream commit changed: state={str(up.get('commit'))[:12]} now={identity.get('short')}")
        if state.get("upstream_inputs_digest") != digest:
            reasons.append("upstream input files changed (content digest differs)")
        if state.get("sync_logic_digest") != logic:
            reasons.append("sync/extract/validate scripts changed since last sync")
        if not (state.get("validation") or {}).get("ok"):
            reasons.append("last recorded validation not ok")
        recorded = state.get("files") or {}
        live = managed_files(root)
        modified = sorted(k for k in recorded if k in live and live[k] != recorded[k])
        missing = sorted(k for k in recorded if k not in live)
        extra = sorted(k for k in live if k not in recorded)
        if modified or missing or extra:
            reasons.append(f"local tree differs from recorded sync: modified={modified[:5]} "
                           f"missing={missing[:5]} extra={extra[:5]}")
    return {"aligned": not reasons, "reasons": reasons, "upstream": identity,
            "upstream_inputs_digest": digest, "root": str(root),
            "state_synced_at": state.get("synced_at")}


def sync(source: Path, root: Path, python: str, cfg: dict, keep: int) -> dict:
    t0 = time.monotonic()
    identity = upstream_identity(source)
    stamp = _now().strftime("%Y%m%d_%H%M%S")
    info = build_staging(source, root, python, identity)
    staging = Path(info["staging"])
    try:
        validation = run_validation(staging, validation_modules(staging, info["closure"]), cfg)
        if not validation["ok"]:
            raise SyncError("候选树验证失败,在用文件未改动:\n" + json.dumps(validation, ensure_ascii=False, indent=1)[:4000])
        _purge_pycache(staging)
        backup = atomic_swap(root, staging, stamp)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    post = run_validation(root, validation_modules(root, info["closure"]), cfg)
    if not post["ok"]:
        rollback(root, backup)
        raise SyncError("换入后在用树验证失败,已回滚到同步前版本:\n" + json.dumps(post, ensure_ascii=False, indent=1)[:4000])
    _purge_pycache(backup)
    removed = prune_backups(root, keep)
    state = {
        "schema_version": 1,
        "synced_at": _now().isoformat(timespec="seconds"),
        "upstream": identity,
        "upstream_inputs_digest": inputs_digest(info["inputs"]),
        "sync_logic_digest": sync_logic_digest(),
        "closure_modules": info["closure_modules"],
        "files_copied": info["files_copied"],
        "extracted": info["extracted"],
        "validation": {"ok": True, "staging": validation, "post_swap": post},
        "backup": str(backup),
        "backups_pruned": removed,
        "elapsed_s": round(time.monotonic() - t0, 1),
        "files": managed_files(root),
    }
    write_state(root, state)
    return state


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    reexec_in_venv(argv)
    sys.path.insert(0, str(SCRIPTS))
    cfg = load_config()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", type=Path, default=None, help="上游工程根(缺省 sync_config.json:source)")
    ap.add_argument("--python", default=None, help="导出 tools_spec 用的解释器(需 agno);缺省 MCP .venv")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="受管 MCP 树(测试/演示可指向副本)")
    ap.add_argument("--keep-backups", type=int, default=KEEP_BACKUPS)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只检查漂移:0 对齐 / 2 漂移 / 1 错误")
    mode.add_argument("--ensure", action="store_true", help="漂移才同步(带验证);失败非 0")
    mode.add_argument("--validate-only", action="store_true", help="只对在用树跑验证(a-e)")
    args = ap.parse_args(argv)
    source = (args.source or Path(cfg.get("source", ""))).expanduser().resolve()
    root = args.root.expanduser().resolve()
    if not (source / "app" / "tools" / "bindings.py").is_file():
        print(f"[sync] FAIL 不是预期的上游工程:{source}", file=sys.stderr)
        return 1
    python = str(Path(args.python).expanduser().absolute()) if args.python else str(VENV_PY)
    try:
        if args.check:
            rep = check(source, root)
            print(json.dumps(rep, ensure_ascii=False, indent=1))
            return 0 if rep["aligned"] else 2
        if args.validate_only:
            closure, _ = import_closure(source)
            res = run_validation(root, validation_modules(root, closure), cfg)
            print(json.dumps(res, ensure_ascii=False, indent=1))
            return 0 if res["ok"] else 1
        if args.ensure:
            rep = check(source, root)
            if rep["aligned"]:
                print(json.dumps({"action": "none", "aligned": True, "upstream": rep["upstream"]["short"],
                                  "synced_at": rep["state_synced_at"]}, ensure_ascii=False))
                return 0
            print(f"[sync] drift → sync: {rep['reasons']}", file=sys.stderr, flush=True)
        state = sync(source, root, python, cfg, args.keep_backups)
        print(json.dumps({
            "action": "synced", "upstream": state["upstream"], "extracted": {
                k: {"roots": v["roots"], "symbols": v["symbols"], "imports": v["imports"]}
                for k, v in state["extracted"].items()},
            "files": len(state["files"]), "backup": state["backup"],
            "probe": state["validation"]["post_swap"].get("d_mcp_stdio", {}).get("tool_call"),
            "elapsed_s": state["elapsed_s"],
        }, ensure_ascii=False, indent=1))
        return 0
    except SyncError as exc:
        print(f"[sync] FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
