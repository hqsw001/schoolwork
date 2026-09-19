# -*- coding: utf-8 -*-
"""静态自检：找出代码里"用了但没导入/没定义"的名字，以及多余的导入。

这个检查是被两个真实缺陷逼出来的——开发中先后出现过 ``human_size``
与 ``K_LIST_DENSITY`` 两处"忘记导入"。它们都属于"只在某条分支上才炸"的
问题：单元测试不覆盖那条分支就发现不了，但用户一用就会看到"内部错误"。

实现方式：带作用域栈的 AST 遍历（而不是简单地收集所有字符串常量——
那样会把字典键、提示文案全当成名字，产生上千条噪音）。

用法：
    python -m tests.static_check
"""

import ast
import builtins
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(PROJECT_ROOT, "app")

#: 允许"导入了但没用"的模块（用于 re-export）
REEXPORT_MODULES = {"__init__.py"}

#: 这些名字由框架注入或属于惯用写法，不检查
IGNORED_NAMES = {
    "__name__", "__file__", "__doc__", "__package__", "__all__",
    "__spec__", "__loader__", "__builtins__", "self", "cls",
    # tkinter 事件回调参数与 ctypes 惯用名
    "event", "args", "kwargs",
}


class Scope(object):
    def __init__(self, kind):
        self.kind = kind          # module / function / class / comprehension
        self.names = set()
        self.loads = set()


class Analyzer(ast.NodeVisitor):
    """带作用域栈的名字解析器。

    规则（够用即可，不追求完整的 Python 作用域语义）：
        - 赋值、导入、函数/类定义、except 别名、global/nonlocal 都算"定义"；
        - for / with / 推导式的目标算"定义"；
        - ``ast.Name`` 的 Load 上下文算"使用"，Attr 的属性名不算；
        - 查名字时从内层往外层找，最后查内置名字。
    """

    def __init__(self, module_name="<module>"):
        self.module_name = module_name
        self.stack = [Scope("module")]
        self.imports = set()
        self.problems = []

    # ------------------------------------------------------------ 工具
    @property
    def current(self):
        return self.stack[-1]

    def define(self, name):
        if name:
            self.current.names.add(name)

    def define_target(self, node):
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                self.define(child.id)

    def resolve(self, name):
        for scope in reversed(self.stack):
            if name in scope.names:
                return True
        return name in dir(builtins)

    # ------------------------------------------------------------ 作用域
    def _visit_function(self, node):
        # 函数名在外层定义；装饰器、默认值、注解都在外层求值
        self.define(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        args = node.args
        for default in list(args.defaults) + [d for d in args.kw_defaults if d]:
            self.visit(default)
        for annotation in self._iter_annotations(args, node.returns):
            self.visit(annotation)

        self.stack.append(Scope("function"))
        self._bind_arguments(args)
        for statement in node.body:
            self.visit(statement)
        self.stack.pop()

    def _iter_annotations(self, args, returns):
        collected = []
        for arg in list(args.args) + list(args.kwonlyargs) + list(args.posonlyargs):
            if arg.annotation is not None:
                collected.append(arg.annotation)
        if args.vararg and args.vararg.annotation is not None:
            collected.append(args.vararg.annotation)
        if args.kwarg and args.kwarg.annotation is not None:
            collected.append(args.kwarg.annotation)
        if returns is not None:
            collected.append(returns)
        return collected

    def _bind_arguments(self, args):
        for arg in list(args.args) + list(args.kwonlyargs) + list(args.posonlyargs):
            self.define(arg.arg)
        if args.vararg:
            self.define(args.vararg.arg)
        if args.kwarg:
            self.define(args.kwarg.arg)

    def visit_FunctionDef(self, node):
        self._visit_function(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d]:
            self.visit(default)
        self.stack.append(Scope("function"))
        self._bind_arguments(node.args)
        self.visit(node.body)
        self.stack.pop()

    def visit_ClassDef(self, node):
        self.define(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        self.stack.append(Scope("class"))
        for statement in node.body:
            self.visit(statement)
        self.stack.pop()

    def visit_comprehension(self, node):
        self.define_target(node.target)
        self.visit(node.iter)
        for condition in node.ifs:
            self.visit(condition)

    def _visit_comprehension_scope(self, node):
        self.stack.append(Scope("comprehension"))
        for generator in node.generators:
            self.visit_comprehension(generator)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self.stack.pop()

    def visit_ListComp(self, node):
        self._visit_comprehension_scope(node)

    def visit_SetComp(self, node):
        self._visit_comprehension_scope(node)

    def visit_GeneratorExp(self, node):
        self._visit_comprehension_scope(node)

    def visit_DictComp(self, node):
        self._visit_comprehension_scope(node)

    # ------------------------------------------------------------ 定义
    def visit_Import(self, node):
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            self.define(name)
            self.imports.add(name)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.name == "*":
                continue
            name = alias.asname or alias.name
            self.define(name)
            self.imports.add(name)

    def visit_ExceptHandler(self, node):
        if node.type is not None:
            self.visit(node.type)
        if node.name:
            self.define(node.name)
        for statement in node.body:
            self.visit(statement)

    def visit_Global(self, node):
        for name in node.names:
            self.stack[0].names.add(name)
            self.define(name)

    def visit_Nonlocal(self, node):
        for name in node.names:
            self.define(name)

    def visit_For(self, node):
        self.visit(node.iter)
        self.define_target(node.target)
        for statement in list(node.body) + list(node.orelse):
            self.visit(statement)

    visit_AsyncFor = visit_For

    def visit_With(self, node):
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self.define_target(item.optional_vars)
        for statement in node.body:
            self.visit(statement)

    visit_AsyncWith = visit_With

    def visit_MatchAs(self, node):  # pragma: no cover - 3.10+ 语法
        if node.pattern is not None:
            self.visit(node.pattern)
        if node.name:
            self.define(node.name)

    # ------------------------------------------------------------ 使用
    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.define(node.id)
            return
        if node.id in IGNORED_NAMES:
            return
        if not self.resolve(node.id):
            self.current.loads.add(node.id)

    def visit_Attribute(self, node):
        # 只看被取属性的对象，属性名本身不需要解析
        self.visit(node.value)

    def visit_Constant(self, node):
        # 字符串注解（"ClipboardEntry" 这种前向引用）不解析，交给运行时
        return

    # ------------------------------------------------------------ 汇总
    def report(self):
        """返回 [(作用域名, 未定义名字)]。"""
        out = []
        for scope in self.stack:
            if scope.loads:
                out.append((scope.kind, sorted(scope.loads)))
        return out


def check_module(path):
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source, filename=path)

    analyzer = Analyzer()
    analyzer.visit(tree)
    undefined = analyzer.report()

    unused = []
    if os.path.basename(path) not in REEXPORT_MODULES:
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr.name if isinstance(node.attr, ast.Name) else "")
                # 形如 a.b.c 时 a 也在 Name 里，已经被收集
        for name in sorted(analyzer.imports):
            if name in IGNORED_NAMES:
                continue
            if name not in used:
                unused.append(name)
    return undefined, unused


def iter_python_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".data")]
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def main():
    problems = 0
    for path in sorted(iter_python_files(APP_DIR)):
        relative = os.path.relpath(path, PROJECT_ROOT)
        try:
            undefined, unused = check_module(path)
        except SyntaxError as exc:
            print("[语法错误] %s: %s" % (relative, exc))
            problems += 1
            continue
        for scope, names in undefined:
            print("[疑似未定义] %s（%s 作用域）" % (relative, scope))
            for name in names:
                print("    - %s" % name)
            problems += len(names)
        if unused:
            print("[未使用导入] %s: %s" % (relative, ", ".join(unused)))
    if problems:
        print("\n发现 %d 处疑似未定义的名字。" % problems)
        return 1
    print("静态自检通过：未发现疑似未定义的名字。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
