# -*- coding: utf-8 -*-
"""静态自检：找出代码里"用了但没导入/没定义"的名字，以及多余的导入。

这个检查是被两个真实缺陷逼出来的——开发中先后出现过 ``human_size``
与 ``K_LIST_DENSITY`` 两处"忘记导入"。它们都属于"只在某条分支上才炸"的
问题：单元测试不覆盖那条分支就发现不了，但用户一用就会看到"内部错误"。

⚠ 2026-09 修订：这个检查器本身出过一个严重的漏报，导致第三个同类缺陷
（``panel.py`` 里 ``SettingsWindow`` 未导入）一路通过回归、直到用户现场
报障"设置打不开"才被发现。原因是 ``report()`` 在**遍历结束之后**才去读
作用域栈，那时所有函数作用域都已经出栈，只剩模块作用域 ——
也就是说**函数体里的未定义名字一个都没检查过**。

现在的做法：
    1. 先做一遍"预扫描"，把模块级/类级绑定的名字全部收集起来
       （见 ``ModuleDefinitionCollector``）。必须先扫完再判定，否则
       ``def f(): return LATER`` 这种合法前向引用会被误报。
    2. 再带作用域栈遍历，并且**在作用域出栈的那一刻**就记下未定义名字，
       而不是等遍历结束再回头找。

用法：
    python -m tests.static_check
"""

import ast
import builtins
import os
import sys
import tempfile

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


class ModuleDefinitionCollector(ast.NodeVisitor):
    """收集模块级与类级绑定的名字（**不进入函数体**）。

    只收模块级/类级，不收函数内部的局部变量——局部名在别的函数里
    是不可见的，收了会把真问题掩盖掉。
    """

    def __init__(self):
        self.names = set()

    # ---- 不进函数体：函数名本身算定义，但函数内部的赋值不算
    def visit_FunctionDef(self, node):
        self.names.add(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        return

    # ---- 类体要进：类属性与方法名对模块级代码是可见的
    def visit_ClassDef(self, node):
        self.names.add(node.name)
        for statement in node.body:
            self.visit(statement)

    def visit_Import(self, node):
        for alias in node.names:
            self.names.add(alias.asname or alias.name.split(".")[0])

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.name != "*":
                self.names.add(alias.asname or alias.name)

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_Global(self, node):
        self.names.update(node.names)

    def visit_ExceptHandler(self, node):
        if node.name:
            self.names.add(node.name)
        for statement in node.body:
            self.visit(statement)


class Scope(object):
    def __init__(self, kind, name=""):
        self.kind = kind          # module / function / class / comprehension
        self.name = name
        self.names = set()
        self.loads = set()


class Analyzer(ast.NodeVisitor):
    """带作用域栈的名字解析器。

    规则（够用即可，不追求完整的 Python 作用域语义）：
        - 赋值、导入、函数/类定义、except 别名、global/nonlocal 都算"定义"；
        - for / with / 推导式的目标算"定义"；
        - ``ast.Name`` 的 Load 上下文算"使用"，Attr 的属性名不算；
        - 查名字时从内层往外层找，最后查内置名字。

    未定义的名字在**作用域出栈时**立刻记录，避免遍历结束后作用域已丢失。
    """

    def __init__(self, module_name="<module>", predefinitions=None):
        self.module_name = module_name
        self.stack = [Scope("module", module_name)]
        if predefinitions:
            # 模块级/类级定义先灌进来，避免把合法前向引用误报为未定义
            self.stack[0].names |= set(predefinitions)
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

    # ------------------------------------------------------------ 作用域栈
    def _push(self, kind, name=""):
        self.stack.append(Scope(kind, name))

    def _pop(self):
        """出栈并记录该作用域里没能解析的名字（关键：不能等到最后再收）。"""
        scope = self.stack.pop()
        if scope.loads:
            label = "%s %s" % (scope.kind, scope.name) if scope.name else scope.kind
            self.problems.append((label, sorted(scope.loads)))
        return scope

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

        self._push("function", node.name)
        self._bind_arguments(args)
        for statement in node.body:
            self.visit(statement)
        self._pop()

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
        self._push("lambda", "<lambda>")
        self._bind_arguments(node.args)
        self.visit(node.body)
        self._pop()

    def visit_ClassDef(self, node):
        self.define(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        self._push("class", node.name)
        for statement in node.body:
            self.visit(statement)
        self._pop()

    def visit_comprehension(self, node):
        self.define_target(node.target)
        self.visit(node.iter)
        for condition in node.ifs:
            self.visit(condition)

    def _visit_comprehension_scope(self, node):
        self._push("comprehension")
        for generator in node.generators:
            self.visit_comprehension(generator)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self._pop()

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
        """返回 [(作用域名, 未定义名字)]。

        注意：这些是在 ``_pop()`` 里逐层攒下来的，不是现在才去栈里翻——
        遍历结束时栈里只剩模块作用域，函数体的问题早就丢了。
        """
        out = list(self.problems)
        module_scope = self.stack[0] if self.stack else None
        if module_scope is not None and module_scope.loads:
            out.append(("module %s" % self.module_name, sorted(module_scope.loads)))
        return out


def check_module(path):
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source, filename=path)

    # 第一遍：收集模块级 / 类级定义
    collector = ModuleDefinitionCollector()
    collector.visit(tree)

    # 第二遍：带作用域栈做名字解析
    analyzer = Analyzer(module_name=os.path.basename(path),
                        predefinitions=collector.names)
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


#: 检查器自检用例：(名字, 源码, 期望抓到的未定义名字集合)
SELFTEST_CASES = [
    # 就是让检查器漏报过一次的那个形状：函数体里用了没导入的类
    ("function_body_nameerror",
     "class P(object):\n"
     "    def go(self):\n"
     "        Missing(self)\n",
     {"Missing"}),
    ("local_typo",
     "def f():\n"
     "    x = 1\n"
     "    return x + typo_here\n",
     {"typo_here"}),
    # 下面是**必须不报**的：报了就是误伤
    ("forward_reference_ok",
     "def f():\n"
     "    return LATER\n"
     "LATER = 1\n",
     set()),
    ("global_ok",
     "N = 0\n"
     "def f():\n"
     "    global N\n"
     "    N += 1\n",
     set()),
    ("builtin_and_import_ok",
     "import os\n"
     "def f(p):\n"
     "    return len(os.path.basename(p))\n",
     set()),
    ("class_attr_ok",
     "class C(object):\n"
     "    LIMIT = 5\n"
     "    def go(self):\n"
     "        return LIMIT\n",
     set()),
]


def selftest():
    """让检查器验一遍自己，返回失败说明列表。

    之所以要有这个自检：检查器本身出过一次**严重漏报** —— report() 在
    遍历结束之后才去读作用域栈，那时函数作用域早已出栈，于是"函数体里
    用了未导入的名字"这一类问题一个都没检查过。panel.py 里
    SettingsWindow 未导入、设置窗口三个入口全部 NameError 的缺陷，
    就是这样一路通过了回归。现在每次运行都先证明它抓得到这类问题。
    """
    failures = []
    for name, source, expected in SELFTEST_CASES:
        path = os.path.join(tempfile.gettempdir(), "_static_selftest_%s.py" % name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(source)
        try:
            undefined, _unused = check_module(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        found = set()
        for _scope, names in undefined:
            found.update(names)
        if found != expected:
            failures.append("%s：期望 %s，实际 %s"
                            % (name, sorted(expected) or "无", sorted(found) or "无"))
    return failures


def main():
    print("--- 检查器自检 ---")
    selftest_failures = selftest()
    if selftest_failures:
        print("检查器自检失败，说明它已经抓不到本该抓到的问题：")
        for item in selftest_failures:
            print("    - %s" % item)
        return 1
    print("检查器自检通过（%d 个用例，该报的报、不该报的不报）\n"
          % len(SELFTEST_CASES))

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
            print("[疑似未定义] %s（%s）" % (relative, scope))
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
