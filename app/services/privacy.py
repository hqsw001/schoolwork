# -*- coding: utf-8 -*-
"""PrivacyService：敏感信息识别与脱敏预览（F9-1 / F9-2 / UC-17）

本期范围（用户已确认本周只做核心链路，隐私模块只做"识别 + 脱敏显示"）：
    ✅ F9-1 敏感信息识别：身份证、手机号、邮箱、密钥类字符串
    ✅ F9-2 预览脱敏显示
    ⏳ F9-3 敏感记录加密存储：留到第三周，本期只在记录上落 is_sensitive 标记，
       写库仍是明文。**这一点在界面与文档里都要如实说明**，
       不能让人以为已经加密了（UC-17 异常流 E2 的定位：本用例是"降低风险"
       而不是"保证拦截"）。

脱敏在**入库前**作用于预览文本与列表展示；用户主动点开时必须能看到原文
（UC-17 业务规则 R3），否则功能就从"隐私保护"变成了"内容丢失"。
"""

import re

#: 敏感类型名（与配置项 app.privacy_protection_kinds 的取值一致）
KIND_ID_CARD = "身份证"
KIND_PHONE = "手机号"
KIND_EMAIL = "邮箱"
KIND_SECRET = "密钥"

ALL_KINDS = (KIND_ID_CARD, KIND_PHONE, KIND_EMAIL, KIND_SECRET)

# ---------------------------------------------------------------------------
# 识别规则
# ---------------------------------------------------------------------------
#: 身份证：18 位，最后一位可为 X。前后加边界，避免把长数字串的一部分当成身份证
RE_ID_CARD = re.compile(r"(?<![0-9A-Za-z])(\d{17}[\dXx])(?![0-9A-Za-z])")

#: 手机号：11 位，1 开头，第二位 3~9
RE_PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")

#: 邮箱
RE_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,})"
)

#: 密钥类：常见前缀令牌 + 长随机串
RE_SECRET_PREFIX = re.compile(
    r"(?<![A-Za-z0-9])((?:sk|pk|rk|ghp|gho|ghs|ghr|glpat|xox[baprs]|AKIA|ASIA|AIza)"
    r"[A-Za-z0-9_\-]{12,})"
)
RE_SECRET_LONG = re.compile(r"(?<![A-Za-z0-9+/=])([A-Za-z0-9+/=_\-]{32,})(?![A-Za-z0-9+/=])")

#: JWT / PEM 私钥这类"一看就知道是密钥"的内容
RE_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


class SensitiveHit(object):
    """一次敏感命中。"""

    __slots__ = ("kind", "matched", "masked")

    def __init__(self, kind, matched, masked):
        self.kind = kind
        self.matched = matched
        self.masked = masked

    def __repr__(self):
        return "<SensitiveHit %s %s>" % (self.kind, self.masked)


class PrivacyService(object):
    def __init__(self, enabled=True, kinds=None, logger=None):
        self.enabled = bool(enabled)
        self.kinds = list(kinds) if kinds else list(ALL_KINDS)
        self.logger = logger
        self._custom_rules = []

    # ------------------------------------------------------------ 配置
    def configure(self, enabled=None, kinds=None):
        if enabled is not None:
            self.enabled = bool(enabled)
        if kinds is not None:
            self.kinds = [k for k in kinds if k in ALL_KINDS]
        return {"enabled": self.enabled, "kinds": list(self.kinds)}

    def add_custom_rule(self, pattern, kind="自定义"):
        """自定义正则规则（UC-17 备选流 A3）。"""
        self._custom_rules.append((kind, re.compile(pattern)))

    # ------------------------------------------------------------ 识别
    def scan(self, text):
        """扫描文本，返回命中的 SensitiveHit 列表（可能为空）。"""
        if not self.enabled or not text:
            return []
        hits = []
        if KIND_ID_CARD in self.kinds:
            for m in RE_ID_CARD.finditer(text):
                hits.append(SensitiveHit(KIND_ID_CARD, m.group(1), mask_id_card(m.group(1))))
        if KIND_PHONE in self.kinds:
            for m in RE_PHONE.finditer(text):
                hits.append(SensitiveHit(KIND_PHONE, m.group(1), mask_phone(m.group(1))))
        if KIND_EMAIL in self.kinds:
            for m in RE_EMAIL.finditer(text):
                hits.append(SensitiveHit(KIND_EMAIL, m.group(1), mask_email(m.group(1))))
        if KIND_SECRET in self.kinds:
            for pattern in (RE_SECRET_PREFIX, RE_SECRET_LONG):
                for m in pattern.finditer(text):
                    token = m.group(1)
                    # 纯数字的长串（例如一段很长的编号）不算密钥，
                    # 否则会把正常内容大面积误脱敏（UC-17 异常流 E1）
                    if token.isdigit():
                        continue
                    hits.append(SensitiveHit(KIND_SECRET, token, mask_secret(token)))
            if RE_PEM.search(text):
                hits.append(SensitiveHit(KIND_SECRET, "PRIVATE KEY", "[私钥内容已隐藏]"))
        for kind, pattern in self._custom_rules:
            for m in pattern.finditer(text):
                value = m.group(0)
                hits.append(SensitiveHit(kind, value, mask_generic(value)))
        return hits

    def is_sensitive(self, text):
        return bool(self.scan(text))

    # ------------------------------------------------------------ 脱敏
    def mask_preview(self, text):
        """生成脱敏后的预览文本（UC-17 主事件流第 5 步）。

        先按位置把命中的片段替换成掩码，再交给调用方做空白折叠与截断。
        """
        if not self.enabled or not text:
            return text
        hits = self.scan(text)
        if not hits:
            return text
        masked = text
        # 长命中优先替换，避免短命中把长命中切碎
        for hit in sorted(hits, key=lambda h: len(h.matched), reverse=True):
            masked = masked.replace(hit.matched, hit.masked)
        return masked

    def describe_hits(self, text):
        """返回命中类型的去重列表，用于卡片上显示"疑似身份证"这类提示。"""
        kinds = []
        for hit in self.scan(text):
            if hit.kind not in kinds:
                kinds.append(hit.kind)
        return kinds


# ---------------------------------------------------------------------------
# 各类掩码规则（保留首尾若干位，中间以星号替换）
# ---------------------------------------------------------------------------
def mask_id_card(value):
    """身份证：保留前 6 位与后 4 位。"""
    if len(value) < 11:
        return mask_generic(value)
    return value[:6] + "*" * (len(value) - 10) + value[-4:]


def mask_phone(value):
    """手机号：保留前 3 位与后 4 位（138****8888）。"""
    if len(value) != 11:
        return mask_generic(value)
    return value[:3] + "****" + value[-4:]


def mask_email(value):
    """邮箱：保留用户名首字符与完整域名（a***@example.com）。"""
    if "@" not in value:
        return mask_generic(value)
    name, _, domain = value.partition("@")
    if len(name) <= 1:
        return "*@" + domain
    return name[0] + "*" * (len(name) - 1) + "@" + domain


def mask_secret(value):
    """密钥类：只显示前缀与长度（sk-abcd…（共 48 位））。"""
    prefix = value[:6]
    return "%s…（共 %d 位）" % (prefix, len(value))


def mask_generic(value):
    """通用掩码：长度大于 4 时保留首尾各 1 位。"""
    if len(value) <= 4:
        return "*" * len(value)
    return value[0] + "*" * (len(value) - 2) + value[-1]
