"""按「符号」而不是「行号」从一个 Python 模块里抽取顶层定义及其依赖闭包。

为什么:旧同步按行号区间逐字摘 `model_router.py` 的一段,上游在区间外加了一行
`from typing import Literal`,摘出来的文件就缺 import,`HarnessConf` 无法构建,
每一次工具调用都失败。按符号抽取时:

1. 调用方只给「根符号」(例如 `HarnessConf`);
2. 用 `ast` + `symtable` 求每个定义真正引用到的模块级名字
   (基类、装饰器、默认值、字段/签名注解——含字符串注解——以及函数/方法体里的全局引用);
3. 引用名若是同模块其它顶层定义(类/函数/常量),递归纳入;
   若由模块的 import 语句绑定(含 `typing.Literal` / `Optional` 等),只保留那条 import 里被用到的别名;
   `from __future__ import ...` 原样保留(它会改变注解语义);
4. 引用了模块顶层对根符号的后置调用(如 `X.model_rebuild()`)也一并带上;
5. **任何解析不了的名字直接抛 `ExtractionError`**——宁可同步失败,也不生成坏文件;
   命中禁止模块(如 `agno`)的 import 同样失败。

输出是逐字源码片段(定义本身按原文复制,不重排不改写),只是按需挑选。
兼容 Python 3.9+(同步脚本会自动切到 MCP 的 3.12 venv 运行)。
"""
from __future__ import annotations

import ast
import builtins
import symtable
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

__all__ = ["ExtractionError", "Extraction", "extract_symbols"]

BUILTIN_NAMES: Set[str] = set(dir(builtins))
MODULE_DUNDERS = {
    "__name__", "__file__", "__doc__", "__package__", "__spec__", "__loader__",
    "__builtins__", "__annotations__", "__dict__", "__module__", "__qualname__", "__class__",
}


class ExtractionError(RuntimeError):
    """符号无法完整、安全地抽取(未解析名字、禁止模块、根符号不存在等)。"""


@dataclass
class Extraction:
    text: str
    roots: List[str]
    symbols: List[str]            # 输出的顶层定义名(源码顺序)
    imports: List[str]            # 输出的 import 语句文本
    trailing_statements: List[str] = field(default_factory=list)


def _is_type_checking(test: ast.AST) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _bound_by_import(node: ast.AST, alias: ast.alias) -> str:
    if alias.asname:
        return alias.asname
    if isinstance(node, ast.Import):
        return alias.name.split(".")[0]
    return alias.name


def _target_names(target: ast.AST) -> List[str]:
    return [n.id for n in ast.walk(target) if isinstance(n, ast.Name)]


def _is_literal_like(func: ast.AST) -> bool:
    name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")
    return name in {"Literal", "Annotated"}


def _expr_names(expr: Optional[ast.AST], *, annotation: bool = False) -> Set[str]:
    """表达式里读取的名字;减去表达式内部 lambda/推导式自己绑定的名字。

    annotation=True 时把字符串常量当前向引用解析(`"HarnessConf"`),但 `Literal[...]`
    的值与 `Annotated[T, ...]` 的元数据不是类型,跳过。
    """
    if expr is None:
        return set()
    loads: Set[str] = set()
    local: Set[str] = set()
    stack: List[Tuple[ast.AST, bool]] = [(expr, annotation)]
    while stack:
        n, ann = stack.pop()
        if isinstance(n, ast.Name):
            if isinstance(n.ctx, ast.Load):
                loads.add(n.id)
            else:
                local.add(n.id)
        elif isinstance(n, ast.arg):
            local.add(n.arg)
        elif ann and isinstance(n, ast.Constant) and isinstance(n.value, str):
            try:
                sub = ast.parse(n.value, mode="eval")
            except SyntaxError:
                continue
            loads |= _expr_names(sub.body, annotation=True)
            continue
        if ann and isinstance(n, ast.Subscript) and _is_literal_like(n.value):
            stack.append((n.value, ann))
            sl = n.slice
            if isinstance(sl, ast.Index):  # py3.8 兼容
                sl = sl.value  # type: ignore[attr-defined]
            if isinstance(n.value, (ast.Name, ast.Attribute)) and (
                (isinstance(n.value, ast.Name) and n.value.id == "Annotated")
                or (isinstance(n.value, ast.Attribute) and n.value.attr == "Annotated")
            ):
                elts = sl.elts if isinstance(sl, ast.Tuple) else [sl]
                if elts:
                    stack.append((elts[0], True))
                for meta in elts[1:]:
                    stack.append((meta, False))
            else:  # Literal[...]:值不是类型,但里面若有名字引用(枚举成员)仍要解析
                stack.append((sl, False))
            continue
        for child in ast.iter_child_nodes(n):
            stack.append((child, ann))
    return loads - local


def _signature_names(fn: ast.AST) -> Set[str]:
    """函数定义语句本身在「外层作用域」求值/被 pydantic 解析的名字。"""
    assert isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    out: Set[str] = set()
    for d in fn.decorator_list:
        out |= _expr_names(d)
    a = fn.args
    for d in list(a.defaults) + [d for d in a.kw_defaults if d is not None]:
        out |= _expr_names(d)
    all_args = list(getattr(a, "posonlyargs", [])) + list(a.args) + list(a.kwonlyargs)
    for extra in (a.vararg, a.kwarg):
        if extra is not None:
            all_args.append(extra)
    for arg in all_args:
        out |= _expr_names(arg.annotation, annotation=True)
    out |= _expr_names(fn.returns, annotation=True)
    return out


def _outer_names(node: ast.AST) -> Set[str]:
    """顶层语句在模块作用域可见的引用:基类/装饰器/默认值/注解(含类体与方法签名)。"""
    out: Set[str] = set()
    if isinstance(node, ast.ClassDef):
        for b in node.bases:
            out |= _expr_names(b)
        for k in node.keywords:
            out |= _expr_names(k.value)
        for d in node.decorator_list:
            out |= _expr_names(d)
        # 类体里的字段注解与方法签名注解:pydantic 在模块命名空间里解析它们
        stack = list(node.body)
        while stack:
            stmt = stack.pop()
            if isinstance(stmt, ast.AnnAssign):
                out |= _expr_names(stmt.annotation, annotation=True)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out |= _signature_names(stmt)
            elif isinstance(stmt, ast.ClassDef):
                stack.extend(stmt.body)
                for b in stmt.bases:
                    out |= _expr_names(b)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        out |= _signature_names(node)
    elif isinstance(node, ast.Assign):
        out |= _expr_names(node.value)
    elif isinstance(node, ast.AnnAssign):
        out |= _expr_names(node.annotation, annotation=True)
        out |= _expr_names(node.value)
    elif isinstance(node, ast.Expr):
        out |= _expr_names(node.value)
    return out


def _table_globals(table: symtable.SymbolTable) -> Set[str]:
    """子作用域(函数/类/lambda/推导式)里解析到模块全局的名字。"""
    out: Set[str] = set()
    for sym in table.get_symbols():
        if sym.is_global():
            out.add(sym.get_name())
    for child in table.get_children():
        out |= _table_globals(child)
    return out


def _inner_bound_names(node: ast.AST) -> Set[str]:
    """定义内部任何位置绑定过的名字(用于容忍类内部局部名出现在注解里)。"""
    out: Set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
            out.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not node:
            out.add(n.name)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
    return out


def _segment_start(lines: Sequence[str], node: ast.AST) -> int:
    """1-based 起始行:含装饰器与紧邻其上的注释块。"""
    start = node.lineno
    for d in getattr(node, "decorator_list", []) or []:
        start = min(start, d.lineno)
    while start > 1 and lines[start - 2].lstrip().startswith("#"):
        start -= 1
    return start


def _render_import(node: ast.AST, aliases: Sequence[ast.alias]) -> str:
    parts = [a.name + (f" as {a.asname}" if a.asname else "") for a in aliases]
    if isinstance(node, ast.Import):
        return "import " + ", ".join(parts)
    assert isinstance(node, ast.ImportFrom)
    return f"from {'.' * node.level}{node.module or ''} import " + ", ".join(parts)


def extract_symbols(
    source: str,
    roots: Iterable[str],
    *,
    filename: str = "<upstream>",
    header: str = "",
    forbidden_modules: Iterable[str] = (),
) -> Extraction:
    roots = list(dict.fromkeys(roots))
    if not roots:
        raise ExtractionError("没有给出任何根符号")
    tree = ast.parse(source, filename=filename)
    lines = source.splitlines()
    module_table = symtable.symtable(source, filename, "exec")
    child_tables: Dict[Tuple[str, int], symtable.SymbolTable] = {
        (t.get_name(), t.get_lineno()): t for t in module_table.get_children()
    }
    forbidden = {m.split(".")[0] for m in forbidden_modules}

    defs: Dict[str, ast.AST] = {}
    imports: Dict[str, Tuple[ast.AST, ast.alias]] = {}
    future: List[ast.AST] = []
    guarded: Set[str] = set()
    opaque: Dict[str, int] = {}
    star_import_line: Optional[int] = None
    trailing_exprs: List[ast.Expr] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            future.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    star_import_line = node.lineno
                    continue
                imports[_bound_by_import(node, alias)] = (node, alias)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            defs[node.name] = node
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for n in _target_names(t):
                    defs[n] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defs[node.target.id] = node
        elif isinstance(node, ast.If) and _is_type_checking(node.test):
            for sub in node.body:
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for alias in sub.names:
                        guarded.add(_bound_by_import(sub, alias))
        elif isinstance(node, ast.Expr):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue  # 模块 docstring
            trailing_exprs.append(node)
        else:
            for n in ast.walk(node):
                if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
                    opaque.setdefault(n.id, node.lineno)
                elif isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                    opaque.setdefault(n.name, node.lineno)
                elif isinstance(n, ast.alias):
                    opaque.setdefault((n.asname or n.name).split(".")[0], node.lineno)

    missing_roots = [r for r in roots if r not in defs]
    if missing_roots:
        raise ExtractionError(f"{filename}: 根符号不是顶层定义:{missing_roots}")

    def refs_of(node: ast.AST) -> Tuple[Set[str], Set[str]]:
        """返回 (必须解析的引用, 可容忍的注解引用候选)。"""
        strict = set()
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            table = child_tables.get((node.name, node.lineno))
            if table is None:
                raise ExtractionError(f"{filename}: 找不到 {node.name} 的符号表")
            strict |= _table_globals(table)
            for d in getattr(node, "decorator_list", []):
                strict |= _expr_names(d)
            if isinstance(node, ast.ClassDef):
                for b in node.bases:
                    strict |= _expr_names(b)
                for k in node.keywords:
                    strict |= _expr_names(k.value)
            soft = _outer_names(node) - strict
            return strict, soft
        return _outer_names(node), set()

    needed_nodes: Dict[int, ast.AST] = {}
    needed_names: List[str] = []
    needed_imports: Dict[int, Tuple[ast.AST, Dict[str, ast.alias]]] = {}
    problems: List[str] = []

    def resolve(owner: str, name: str, node: ast.AST, *, soft: bool, queue: List[str]) -> None:
        if name in defs:
            queue.append(name)
        elif name in imports:
            imp_node, alias = imports[name]
            mod = alias.name if isinstance(imp_node, ast.Import) else (imp_node.module or "")
            if imp_node.__class__ is ast.ImportFrom and imp_node.level:
                mod = "." * imp_node.level + mod
            if mod.lstrip(".").split(".")[0] in forbidden:
                problems.append(f"{owner} 需要 `{name}`,它来自禁止模块 `{mod}`")
                return
            needed_imports.setdefault(id(imp_node), (imp_node, {}))[1][alias.name + "|" + str(alias.asname)] = alias
        elif name in BUILTIN_NAMES or name in MODULE_DUNDERS:
            return
        elif soft and name in _inner_bound_names(node):
            return
        elif name in guarded:
            problems.append(f"{owner} 引用 `{name}`,它只在 `if TYPE_CHECKING:` 下导入,运行期不可用")
        elif name in opaque:
            problems.append(
                f"{owner} 引用 `{name}`,它由第 {opaque[name]} 行的复合顶层语句绑定(if/try 等),抽取器不支持"
            )
        elif star_import_line is not None:
            problems.append(f"{owner} 引用 `{name}`,可能来自第 {star_import_line} 行的 `import *`,无法确定")
        else:
            problems.append(f"{owner} 引用了无法解析的名字 `{name}`")

    queue: List[str] = list(roots)
    while True:
        while queue:
            name = queue.pop()
            node = defs[name]
            if id(node) in needed_nodes:
                continue
            needed_nodes[id(node)] = node
            strict, soft = refs_of(node)
            for ref in sorted(strict):
                resolve(name, ref, node, soft=False, queue=queue)
            for ref in sorted(soft):
                resolve(name, ref, node, soft=True, queue=queue)
        # 顶层后置语句(如 `X.model_rebuild()`)若作用于已纳入的定义,一并纳入
        kept_names = {n for n, nd in defs.items() if id(nd) in needed_nodes}
        added = False
        for expr in trailing_exprs:
            if id(expr) in needed_nodes:
                continue
            names = _expr_names(expr.value)
            if names & kept_names:
                needed_nodes[id(expr)] = expr
                added = True
                for ref in sorted(names):
                    resolve(f"第{expr.lineno}行语句", ref, expr, soft=False, queue=queue)
        if not added and not queue:
            break

    if problems:
        raise ExtractionError(f"{filename}: 符号抽取失败(不生成文件):\n  - " + "\n  - ".join(dict.fromkeys(problems)))

    ordered = sorted(needed_nodes.values(), key=lambda n: n.lineno)
    symbols: List[str] = []
    for n in ordered:
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                symbols.extend(_target_names(t))
        elif isinstance(n, ast.AnnAssign):
            symbols.append(n.target.id)  # type: ignore[union-attr]

    out: List[str] = []
    if header:
        out.append(header.rstrip("\n") + "\n")
    for f in future:
        out.append(ast.get_source_segment(source, f) + "\n")
    import_lines: List[str] = []
    for imp_node, used in sorted(needed_imports.values(), key=lambda x: x[0].lineno):
        wanted = [a for a in imp_node.names if (a.name + "|" + str(a.asname)) in used]
        if len(wanted) == len(imp_node.names):
            text = ast.get_source_segment(source, imp_node)
        else:
            text = _render_import(imp_node, wanted)
        import_lines.append(text)
    if import_lines:
        out.append("\n" + "\n".join(import_lines) + "\n")
    trailing_texts: List[str] = []
    for n in ordered:
        start = _segment_start(lines, n)
        seg = "\n".join(lines[start - 1:n.end_lineno])  # type: ignore[attr-defined]
        if isinstance(n, ast.Expr):
            trailing_texts.append(seg)
        out.append("\n\n" + seg + "\n")
    text = "".join(out)
    compile(text, filename + " (extracted)", "exec")  # 语法兜底
    return Extraction(text=text, roots=roots, symbols=symbols, imports=import_lines,
                      trailing_statements=trailing_texts)
