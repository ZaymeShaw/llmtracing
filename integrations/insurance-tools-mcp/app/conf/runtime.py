"""环境配置与按轮快照；显式启用公共配置合并，不创建后台监听线程。"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Callable, Iterator
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps, lru_cache
from pathlib import Path
from threading import RLock
from typing import cast, Any, TypeVar

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ValidationError
from app.conf.readonly import freeze

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
MAX_CONFIG_BYTES = 1024 * 1024


class AppConfigError(ValueError):
    """配置异常只携带安全类别，不包含原文、路径或凭据。"""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(f"APP_CONFIG_INVALID: {category}")


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        result: dict = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise AppConfigError("yaml")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def parse_yaml(content: bytes) -> dict:
    try:
        payload = yaml.load(content.decode("utf-8"), Loader=_UniqueKeyLoader)
    except (UnicodeError, yaml.YAMLError, RecursionError):
        raise AppConfigError("yaml") from None
    if not isinstance(payload, dict):
        raise AppConfigError("model")
    return payload


def _file_signature(path: Path) -> tuple[int, ...]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@lru_cache(maxsize=256)
def _stable_bytes(path: Path, signature: tuple[int, ...]) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(MAX_CONFIG_BYTES + 1)
    if len(content) > MAX_CONFIG_BYTES:
        raise AppConfigError("size")
    if _file_signature(path) != signature:
        raise AppConfigError("changed")
    return content


def read_bytes(path: Path) -> bytes:
    try:
        return _stable_bytes(path, _file_signature(path))
    except OSError:
        raise AppConfigError("read") from None


@dataclass(frozen=True)
class ConfigSource:
    env_type: str
    directory: Path
    capabilities_directory: Path | None = None

    def path(self, name: str) -> Path:
        relative = Path(name)
        directory = relative.parent if len(relative.parts) > 1 else relative
        return self.directory / directory / f"{relative.name}_{self.env_type}_args.yaml"

    def common_path(self, name: str) -> Path:
        return self.path(name).with_name(f"{Path(name).name}_common_args.yaml")


@dataclass
class ConfigSnapshot:
    source: ConfigSource
    # Validated models and nested containers are frozen once per snapshot.
    strict: bool = False
    values: dict[str, BaseModel] = field(default_factory=dict, repr=False)
    derived: dict[str, Any] = field(default_factory=dict, repr=False)


_SOURCE: ContextVar[ConfigSource | None] = ContextVar("app_config_source", default=None)
_SNAPSHOT: ContextVar[ConfigSnapshot | None] = ContextVar("app_config_snapshot", default=None)
_HOT_VALUES: ContextVar[dict | None] = ContextVar("request_readonly_config", default=None)


@contextmanager
def readonly_config_scope() -> Iterator[None]:
    """请求内只读配置复用；消费者不得修改嵌套值，更新应使用model_copy。"""
    if _HOT_VALUES.get() is not None:
        yield
        return
    token = _HOT_VALUES.set({})
    try:
        yield
    finally:
        _HOT_VALUES.reset(token)


def read_once(key: object, loader: Callable[[], T]) -> T:
    values = _HOT_VALUES.get()
    if values is None:
        return loader()
    # 嵌套应用/快照不能复用另一个配置源；缓存只活在当前请求内。
    cache_key = (id(_SNAPSHOT.get()), key)
    if cache_key not in values:
        values[cache_key] = (_SNAPSHOT.get(), loader())
    return cast(T, values[cache_key][1])
_CACHE: dict[tuple[tuple[Path, ...], type[BaseModel]], tuple[str, BaseModel]] = {}
_FAILURES: dict[tuple[tuple[Path, ...], type[BaseModel]], str] = {}
_ACTIVE_REVISIONS: dict[tuple[tuple[Path, ...], type[BaseModel]], str] = {}
_LOCK = RLock()


D = TypeVar("D")


def snapshot_derived(key: str, factory: Callable[[], D]) -> D:
    """Cache read-only derived objects for exactly the owning snapshot lifetime."""
    snapshot = _SNAPSHOT.get()
    if snapshot is None:
        return factory()
    if key not in snapshot.derived:
        snapshot.derived[key] = factory()
    return cast(D, snapshot.derived[key])


def current_source() -> ConfigSource:
    source = _SOURCE.get()
    if source is not None:
        return source
    from app.conf import CONF_DIR
    from app.conf.tool_config_loader import resolve_env_type
    from app.conf.capabilities import CAPABILITIES_DIR

    return ConfigSource(resolve_env_type(os.environ.get("ENV_TYPE")), CONF_DIR.resolve(), CAPABILITIES_DIR.resolve())


@contextmanager
def config_scope(snapshot: ConfigSnapshot | None) -> Iterator[None]:
    if snapshot is None:
        yield
        return
    source_token = _SOURCE.set(snapshot.source)
    snapshot_token = _SNAPSHOT.set(snapshot)
    try:
        yield
    finally:
        _SNAPSHOT.reset(snapshot_token)
        _SOURCE.reset(source_token)


def application_environment(fn: Callable[..., Any]) -> Callable[..., Any]:
    """装配时固定环境，离开装配后恢复上下文，不污染同进程的另一个应用。"""
    @wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with config_scope(ConfigSnapshot(current_source(), strict=True)):
            return fn(*args, **kwargs)
    return wrapped


def workflow_configured(fn: Callable[..., Any]) -> Callable[..., Any]:
    """在异步调用者所在任务绑定快照，覆盖执行守卫及执行后审计。"""
    @wraps(fn)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        with config_scope(getattr(kwargs.get("workflow"), "config_snapshot", None)):
            return await fn(*args, **kwargs)
    return wrapped


def workflow_configured_stream(fn: Callable[..., Any]) -> Callable[..., Any]:
    """流消费任务自己绑定并恢复快照，不能跨任务重置 ContextVar token。"""
    @wraps(fn)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        with config_scope(getattr(kwargs.get("workflow"), "config_snapshot", None)):
            async with aclosing(fn(*args, **kwargs)) as stream:
                async for event in stream:
                    yield event
    return wrapped


def _merge_settings(common: dict, environment: dict) -> dict:
    """字典递归覆盖，列表/标量/null 整体替换；null 是否合法由最终 schema 判定。"""
    merged = dict(common)
    for key, value in environment.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _merge_settings(merged[key], value)
        else:
            merged[key] = value
    return merged


_KNOWN_LAYERS: set[Path] = set()


def layer_paths(path: Path) -> tuple[Path, ...]:
    """只读取同组公共文件与选定环境，兼容显式提供的完整独立配置。"""
    common = path.with_name(re.sub(r"_(dev|stg|prd)_args\.yaml$", "_common_args.yaml", path.name))
    with _LOCK:
        if common != path and common not in _KNOWN_LAYERS and common.exists():
            _KNOWN_LAYERS.add(common)
        # 已加载的公共层消失必须触发 read 错误并保留旧快照，不能悄悄切换布局。
        return (common, path) if common != path and common in _KNOWN_LAYERS else (path,)


def layer_payload(content: dict[Path, bytes], path: Path) -> dict:
    common = path.with_name(re.sub(r"_(dev|stg|prd)_args\.yaml$", "_common_args.yaml", path.name))
    payload = parse_yaml(content[path])
    return _merge_settings(parse_yaml(content[common]), payload) if common != path and common in content else payload


def load_model(
    name: str, model: type[T], *, reload: bool = False, common: bool | None = None,
    environment_only: tuple[str, ...] = (),
) -> T:
    """已迁移组显式读取公共文件 + 当前环境文件；两文件均必需，整体校验和缓存。"""
    source = current_source()
    path = source.path(name).absolute()
    paths = (source.common_path(name).absolute(), path) if common else (layer_paths(path) if common is None else (path,))

    def prepare(content: dict[Path, bytes]) -> T:
        payload = parse_yaml(content[path])
        if len(paths) > 1:
            defaults = parse_yaml(content[paths[0]])
            # 地址/凭据等环境专属字段不能从公共文件继承，也不能漏填当前环境。
            if any(key in defaults or key not in payload for key in environment_only):
                raise AppConfigError("environment_fields")
            payload = _merge_settings(defaults, payload)
        return model.model_validate(payload, strict=True)

    return load_bundle(name, model, paths, prepare, reload=reload)


def _validation_rules(exc: Exception, model: type[BaseModel]) -> list[dict]:
    """Log rule identifiers and schema field paths; never arbitrary map keys, values or messages."""
    if not isinstance(exc, ValidationError):
        return [{"rule": exc.category if isinstance(exc, AppConfigError) else "model"}]
    known_fields = set(model.model_fields)
    try:
        pending = [model.model_json_schema()]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                known_fields.update(node.get("properties", {}))
                pending.extend(node.values())
            elif isinstance(node, list):
                pending.extend(node)
    except Exception:
        pass  # A diagnostic schema failure must not prevent retaining valid configuration.
    rules = []
    for error in exc.errors(include_input=False, include_context=True, include_url=False):
        location = error.get("loc") or ()
        field = ".".join(str(part) if part in known_fields else "<key>" for part in location) or "<model>"
        rule: dict = {"field": field, "rule": error["type"]}
        # Numeric limits help locate the violated rule; never log custom exception messages/inputs.
        bounds = {k: v for k, v in (error.get("ctx") or {}).items()
                  if k in {"gt", "ge", "lt", "le", "min_length", "max_length"} and type(v) in (int, float)}
        if bounds:
            rule["bounds"] = bounds
        if rule not in rules:
            rules.append(rule)
    return rules


def load_bundle(
    name: str, model: type[T], paths: tuple[Path, ...],
    prepare: Callable[[dict[Path, bytes]], T], *, reload: bool = True,
) -> T:
    """多个独立文件完成交叉校验后整体发布；不把单文件候选提前放进缓存。"""
    snapshot = _SNAPSHOT.get() if reload else None
    if snapshot is not None and name in snapshot.values:
        return cast(T, snapshot.values[name])
    source = current_source()
    key = (tuple(path.absolute() for path in paths), model)
    with _LOCK:
        previous = _CACHE.get(key) if reload and not (snapshot is not None and snapshot.strict) else None
        revision = "unreadable"
        try:
            content = {path: read_bytes(path) for path in paths}
            # 长度分隔防止相邻文件拼接歧义，解析和摘要仍来自同次读取的字节。
            digest = hashlib.sha256()
            for payload in content.values():
                digest.update(len(payload).to_bytes(8, "big"))
                digest.update(payload)
            revision = digest.hexdigest()
            if previous is not None and previous[0] == revision:
                result = cast(T, previous[1].model_copy(deep=True))
            else:
                result = prepare(content)
                if len(paths) > 1 and any(read_bytes(path) != content[path] for path in paths):
                    raise AppConfigError("changed")
                if reload:
                    _CACHE[key] = (revision, result.model_copy(deep=True))
            if _ACTIVE_REVISIONS.get(key) != revision:
                logger.info("app_config_effective", extra={"observability": {
                    "env_type": source.env_type, "operation": name,
                    "effective_revision": revision, "outcome": "published",
                }})
                _ACTIVE_REVISIONS[key] = revision
            _FAILURES.pop(key, None)
        except (AppConfigError, ValidationError, ValueError, UnicodeError, RecursionError) as exc:
            category = exc.category if isinstance(exc, AppConfigError) else "model"
            if _FAILURES.get(key) != revision:
                logger.warning("app_config_reload", extra={"observability": {
                    "env_type": source.env_type, "category": category, "operation": name,
                    "outcome": "retained" if previous is not None else "rejected",
                    "retained_revision": previous[0] if previous is not None else None,
                    "rejected_revision": revision, "rules": _validation_rules(exc, model),
                }})
                _FAILURES[key] = revision
            if previous is None:
                raise AppConfigError(category) from None
            result = cast(T, previous[1].model_copy(deep=True))
        if snapshot is not None:
            result = freeze(result)
            snapshot.values[name] = result
        return result
