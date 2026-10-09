# -*- coding: utf-8 -*-
"""程序路径解析：把"源码运行"与"打包成 exe 运行"这两种形态统一在这里判断。

为什么需要单独一层：打包（PyInstaller 的 frozen 形态）之后有三处和源码运行
不一样，任何一处算错都会出真问题：

1. ``__file__`` 指向的是**解包目录**（``sys._MEIPASS``；onedir 下就是
   ``_internal``），不是用户看到的程序目录。拿它推数据目录，历史记录会写进
   ``_internal\\.data`` —— 用户把文件夹换个位置，或者直接用新版覆盖旧版，
   数据看起来就"丢了"。所以**数据目录必须按 exe 所在目录算**。
2. ``sys.executable`` 变成 exe 自己（源码运行时是 ``python.exe``）。
   开机自启、重启这些地方必须跟着变，否则注册进注册表的是一条错命令。
3. 随程序带的资源（``app/data/emoji_data.json``）躺在解包目录里，
   要按 ``sys._MEIPASS`` 拼路径，不能按 exe 所在目录拼。

三种"根"因此要分清楚，本模块把它们各自暴露成一个函数：

    program_root()   程序根（源码=项目根；打包=exe 所在目录）→ 放 ``.data``
    bundle_root()    资源根（源码=项目根；打包=解包目录）→ 放随包资源
    executable_path() 可执行文件（源码=python.exe；打包=exe 自己）
"""

import os
import sys


def is_frozen():
    """是否由打包器（PyInstaller 等）启动。

    判据用 ``sys.frozen``：PyInstaller / cx_Freeze / py2exe 都会设置它。
    """
    return bool(getattr(sys, "frozen", False))


def program_root():
    """程序根目录。

    - 源码运行：项目根（``<项目>/app/infrastructure/paths.py`` 上溯三级）
    - 打包之后：**exe 所在目录**

    数据目录 ``.data`` 就挂在它下面，保证"绿色便携、整个文件夹拷走即迁移"，
    也保证换新版覆盖 exe 时历史记录还在原地。
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def bundle_root():
    """随程序打包的资源根目录（``app/`` 包的上一级）。

    - 源码运行：项目根
    - 打包之后：``sys._MEIPASS``（onedir 下是 exe 同级的 ``_internal``）
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return os.path.abspath(meipass)
    if is_frozen():
        # 极少数打包器不设 _MEIPASS，退回到 exe 所在目录
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def executable_path():
    """本程序的可执行文件路径。

    源码运行时是 ``python.exe``；打包之后是 exe 自己。写进注册表的自启命令、
    "重启本程序"用的命令都以它为准。
    """
    return sys.executable or ""


def bundle_file(*parts):
    """按 ``bundle_root()`` 拼一个随包资源路径。"""
    return os.path.join(bundle_root(), *parts)
