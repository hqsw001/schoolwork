# -*- coding: utf-8 -*-
"""应用级规则引擎（对应 TieZ 的 ``AppCleanupPolicy``）

两类规则：

* **忽略采集**（``action="ignore"``）——来自某个应用的复制根本不入库。
  典型场景：密码管理器、银行客户端。
* **自动清洗**（``action="clean"``）——来自某个应用的内容入库前先做正则替换。
  典型场景：从 IDE 复制代码时去掉行号。

规则的匹配目标同时支持四种写法（与参考实现一致）：

====================  ==========================================
``C:\\App\\a.exe``   完整路径，大小写不敏感
``a.exe``           可执行文件名（会从路径里取 basename 再比）
``/正则/``          斜杠包裹时按正则匹配
``Pkg_1.0_x64!App`` MSIX/AUMID，按包名+发布者匹配进程路径
====================  ==========================================

清洗规则与全局规则共用同一种文本格式（与参考实现一致）::

    # 这行是注释，会被忽略
    ^\\s*\\d+\\t  =>
    旧词 => 新词

替换值里的 ``\\n`` ``\\r`` ``\\t`` 会被还原成真正的换行/制表符——
这样用户才能用一行规则把多行内容压成一行。
"""

import json
import os
import re

#: 动作
ACTION_IGNORE = "ignore"
ACTION_CLEAN = "clean"
ACTIONS = (ACTION_IGNORE, ACTION_CLEAN)

#: 忽略采集的魔法串（参考实现里也是这个值，保持一致）
IGNORE_SENTINEL = "__IGNORE_CAPTURE__"

#: 内容类型全集的默认值（策略不指定 content_types 时按"全部"处理）
DEFAULT_CONTENT_TYPES = ("text", "code", "url", "rich_text", "image", "file",
                         "video")

#: 清洗规则只对这些类型生效。图片/文件类的 content 是磁盘路径，
#: 拿文本正则去替换它只会把路径改坏。
TEXT_CONTENT_TYPES = ("text", "code", "url", "rich_text")

#: 内容类型的中文名（界面摘要用）
TYPE_LABELS = {
    "text": "文本", "code": "代码", "url": "网址",
    "rich_text": "富文本", "image": "图片", "file": "文件", "video": "视频",
}


def describe_content_types(kinds):
    """把内容类型列表说成人话。"""
    labels = [TYPE_LABELS.get(str(k).lower(), str(k)) for k in (kinds or [])]
    return "、".join(labels)

#: 单条策略里清洗规则的条数上限（防止一条策略把采集拖垮）
MAX_RULES_PER_POLICY = 200


# ---------------------------------------------------------------------------
# 清洗规则：解析与应用
# ---------------------------------------------------------------------------
def parse_cleanup_rules(raw, logger=None):
    """把多行文本解析成 [(正则, 替换), ...]。

    逐行处理：跳过空行与 ``#`` 注释；没有 ``=>`` 的行跳过；
    正则写错的行跳过并记日志——**一条写错的规则不能让整块采集失效**。
    """
    rules = []
    if not raw:
        return rules
    for line in str(raw).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" not in line:
            continue
        pattern, _, replacement = line.partition("=>")
        pattern = pattern.strip()
        if not pattern:
            continue
        # 还原转义：让用户能用一行规则插入换行/制表符
        replacement = (replacement.strip()
                       .replace("\\n", "\n")
                       .replace("\\r", "\r")
                       .replace("\\t", "\t"))
        try:
            rules.append((re.compile(pattern), replacement))
        except re.error as exc:
            if logger:
                logger.warning("清洗规则 %r 无效，已忽略：%s", line[:60], exc)
        if len(rules) >= MAX_RULES_PER_POLICY:
            if logger:
                logger.warning("清洗规则超过 %d 条，多余部分已忽略", MAX_RULES_PER_POLICY)
            break
    return rules


def apply_cleanup_rules(text, rules):
    """依次应用规则。没命中任何规则时原样返回。"""
    if not text or not rules:
        return text
    result = text
    for pattern, replacement in rules:
        try:
            result = pattern.sub(replacement, result)
        except re.error:
            continue
    return result


# ---------------------------------------------------------------------------
# 应用匹配
# ---------------------------------------------------------------------------
def normalize_executable_name(value):
    """取可执行文件名（小写）。空值或不含 .exe 的返回 None。

    为什么要判 ``.exe``：策略里写 "a.exe" 才当作文件名来比，
    否则 "记事本" 这种中文应用名会被误当成文件名。
    """
    if not value:
        return None
    text = str(value).strip().strip('"')
    if not text:
        return None
    name = os.path.basename(text.replace("/", "\\"))
    if "." not in name:
        return None
    return name.lower()


def executable_name_matches(left, right):
    """两边的可执行文件名是否相同。"""
    first = normalize_executable_name(left)
    second = normalize_executable_name(right)
    return bool(first and second and first == second)


def app_id_matches_process_path(app_id, process_path):
    """MSIX/AUMID（``包名_发布者!App``）是否对应这个进程路径。"""
    if not app_id or not process_path:
        return False
    app_id = str(app_id).strip()
    process_path = str(process_path).strip()
    if not app_id or not process_path or "!" not in app_id:
        return False
    package_family = app_id.split("!", 1)[0].strip()
    if "_" not in package_family:
        return False
    package_name, _, publisher_id = package_family.rpartition("_")
    normalized = process_path.replace("/", "\\").lower()
    package_name = package_name.strip().lower()
    publisher_id = publisher_id.strip().lower()
    return bool(package_name and publisher_id
                and package_name in normalized and publisher_id in normalized)


def _field_matches(pattern, candidate, allow_aumid=False):
    """单个字段的匹配：``/正则/`` → 正则；否则精确 / 文件名 / AUMID。"""
    pattern = (pattern or "").strip()
    candidate = (candidate or "").strip()
    if not pattern or not candidate:
        return False

    if len(pattern) >= 2 and pattern.startswith("/") and pattern.endswith("/"):
        body = pattern[1:-1]
        try:
            return bool(re.search(body, candidate))
        except re.error:
            return False

    if pattern.lower() == candidate.lower():
        return True
    if executable_name_matches(pattern, candidate):
        return True
    if allow_aumid and app_id_matches_process_path(pattern, candidate):
        return True
    return False


def _content_type_allowed(policy, content_type):
    """内容类型过滤。

    ``ignore`` 策略不写类型时表示"这个应用的全部内容都忽略"；
    ``clean`` 策略不写类型时表示"不匹配任何内容"——因为清洗规则是针对
    具体内容写的，没说清楚就不能乱套用。
    """
    kinds = [str(k).strip().lower() for k in (policy.get("contentTypes") or []) if k]
    if not kinds:
        return policy.get("action") == ACTION_IGNORE
    return str(content_type or "").strip().lower() in kinds


def app_cleanup_policy_matches(policy, source_app, source_app_path, content_type):
    """这条策略是否命中当前这次采集。"""
    if not policy or not policy.get("enabled", True):
        return False
    if not _content_type_allowed(policy, content_type):
        return False

    app_path = (policy.get("appPath") or "").strip()
    app_name = (policy.get("appName") or "").strip()
    source_app = (source_app or "").strip()
    source_app_path = (source_app_path or "").strip()

    # 路径字段：优先与进程路径比，再退回与来源应用名比
    if app_path:
        if source_app_path and _field_matches(app_path, source_app_path, allow_aumid=True):
            return True
        if source_app and _field_matches(app_path, source_app):
            return True

    # 名称字段：两边都试——用户可能把路径填在名字里，也可能反过来
    if app_name:
        if source_app and _field_matches(app_name, source_app):
            return True
        if source_app_path and _field_matches(app_name, source_app_path):
            return True

    return False


def parse_app_cleanup_policies(raw, logger=None):
    """解析策略列表（JSON 文本）。坏数据一律忽略，绝不抛异常。"""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        data = list(raw)
    else:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            if logger:
                logger.warning("应用规则不是合法 JSON，已忽略：%s", exc)
            return []
    if not isinstance(data, list):
        return []

    policies = []
    for item in data:
        if not isinstance(item, dict):
            continue
        policy = normalize_policy(item)
        # 没有匹配目标的策略是无意义的（参考实现也这么过滤）
        if not policy["appPath"] and not policy["appName"]:
            continue
        policies.append(policy)
    return policies


def normalize_policy(item):
    """补全字段、纠正取值，保证后续使用不必再做防御。"""
    action = str(item.get("action") or ACTION_CLEAN).strip().lower()
    if action not in ACTIONS:
        action = ACTION_CLEAN
    kinds = item.get("contentTypes")
    if not isinstance(kinds, (list, tuple)):
        kinds = list(DEFAULT_CONTENT_TYPES)
    return {
        "id": str(item.get("id") or ""),
        "enabled": bool(item.get("enabled", True)),
        "appName": str(item.get("appName") or ""),
        "appPath": str(item.get("appPath") or ""),
        "action": action,
        "contentTypes": [str(k).strip() for k in kinds if str(k).strip()],
        "cleanupRules": str(item.get("cleanupRules") or ""),
    }


def dump_app_cleanup_policies(policies):
    """策略列表 → JSON 文本（写回配置用）。"""
    return json.dumps([normalize_policy(p) for p in (policies or [])],
                      ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
class AppRuleEngine(object):
    """把策略列表包装成一个"查一次就知道怎么办"的对象。"""

    def __init__(self, raw="", logger=None):
        self.logger = logger
        self._policies = parse_app_cleanup_policies(raw, logger)

    def reload(self, raw):
        self._policies = parse_app_cleanup_policies(raw, self.logger)
        return len(self._policies)

    def policies(self):
        return list(self._policies)

    def count(self):
        return len(self._policies)

    def match(self, source_app, source_app_path, content_type):
        """返回第一条命中的策略；没有则 None。顺序即优先级。"""
        for policy in self._policies:
            if app_cleanup_policy_matches(policy, source_app, source_app_path,
                                          content_type):
                return policy
        return None

    def evaluate(self, source_app, source_app_path, content_type, text):
        """对一次采集给出裁决。

        返回 ``(action, text, policy)``：

        * ``action`` 为 ``None`` —— 不干预
        * ``action`` 为 ``"ignore"`` —— 这次复制不入库
        * ``action`` 为 ``"clean"`` —— 用清洗后的 ``text`` 继续
        """
        policy = self.match(source_app, source_app_path, content_type)
        if policy is None:
            return None, text, None
        if policy["action"] == ACTION_IGNORE:
            return ACTION_IGNORE, text, policy

        # 清洗只对文本类生效。图片/文件类的 content 是磁盘路径，
        # 拿文本正则去替换它只会把路径改坏。这道闸放在**引擎里**而不是
        # 只在管线里：否则试跑、脚本调用这些旁路会绕过它，
        # 出现"试跑说会清洗、实际管线不清洗"的不一致。
        if content_type not in TEXT_CONTENT_TYPES:
            return None, text, policy

        rules = parse_cleanup_rules(policy.get("cleanupRules"), self.logger)
        if not rules:
            # 写了 clean 却没有任何有效规则：不干预。
            # 把内容清空是"静默丢数据"，比不生效糟糕得多。
            return None, text, policy
        return ACTION_CLEAN, apply_cleanup_rules(text, rules), policy
