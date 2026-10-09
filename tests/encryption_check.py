# -*- coding: utf-8 -*-
"""隐私加密落盘回归（差距清单批次 9 / F9-3）

对应 TieZ 的 ``encrypt.rs``（Windows DPAPI）与"敏感标签即加密"的联动。

本套件盯住六个容易写错的地方：

1. **加密是透明的。** 库里必须是密文，读出来必须是明文。任何一条读路径
   忘了解密，用户就会看到一串 base64。
2. **加密之后还得搜得到。** 正文是密文时 SQL 的 LIKE 天然匹配不到，
   只走 SQL 的话"搜自己的手机号却搜不到"会让人以为记录丢了。
3. **图片/文件类不能加密。** 它们的 content 是磁盘路径，加密之后附件清理、
   打开内容、迁移重写路径全对不上。
4. **去掉敏感标签不能撤掉识别器给出的保护。** 内容本身是身份证号，
   删个标签就变回明文，那是把保护做成了摆设。
5. **标记与落盘形态必须一致。** 只改标志位不改内容是烂账。
6. **加解密失败要有明确的占位说明**，而不是留一串 base64。

用法：python -m tests.encryption_check
"""

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.app.kernel import Kernel  # noqa: E402
from app.domain.models import ClipboardEntry  # noqa: E402
from app.infrastructure import encryption as enc  # noqa: E402
from app.services import privacy as pv  # noqa: E402

PASSED = []
FAILED = []

#: 一条含身份证号的内容，用来触发敏感识别
ID_CARD_TEXT = "客户的身份证 110101199003074567 请保管好"
PLAIN_TEXT = "这是一段完全普通的文本内容，没有任何敏感信息"


def check(condition, message):
    if condition:
        PASSED.append(message)
        print("  [OK]   %s" % message)
    else:
        FAILED.append(message)
        print("  [FAIL] %s" % message)


def raw_content(db_path, entry_id):
    """直接读数据库里的原始字节（绕过仓储层的解密）。"""
    uri = "file:%s?mode=ro" % db_path.replace("\\", "/")
    conn = sqlite3.connect(uri, uri=True)
    try:
        row = conn.execute(
            "SELECT content FROM clipboard_history WHERE id = ?", (entry_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def main():
    if not enc.is_available():
        print("当前平台不支持 DPAPI，无法运行本套件")
        return 1

    # ==================================================================
    print("=== 1) DPAPI 加解密本身 ===")
    check(enc.is_available() is True, "DPAPI 可用")
    for sample in ("13812345678", ID_CARD_TEXT, "中文也要能加密", "a" * 3000):
        cipher = enc.encrypt(sample)
        check(cipher is not None and cipher.startswith(enc.ENCRYPT_PREFIX),
              "加密后带 dpapi: 前缀（%d 字符的明文）" % len(sample))
        check(enc.decrypt(cipher) == sample, "解密往返一致（%d 字符）" % len(sample))
        check(sample not in cipher, "明文内容不在密文里")

    first, second = enc.encrypt("同样的内容"), enc.encrypt("同样的内容")
    check(first != second, "同一明文两次加密结果不同（DPAPI 带随机盐）")
    check(enc.decrypt(first) == enc.decrypt(second) == "同样的内容", "但都能解回原文")

    check(enc.is_encrypted("dpapi:x") is True, "is_encrypted 认前缀")
    check(enc.is_encrypted("普通文本") is False, "明文不算密文")

    print("\n=== 2) 异常输入不能炸 ===")
    for bad in (None, "", "dpapi:", "dpapi:!!!不是base64", "dpapi:" + "A" * 200, "明文"):
        try:
            result = enc.decrypt(bad)
            check(result is None, "非法密文返回 None：%r" % (bad[:16] if bad else bad))
        except Exception as exc:  # noqa: BLE001
            check(False, "非法密文 %r 抛了异常：%s" % (bad, exc))
    check(enc.encrypt(None) is None, "加密 None 返回 None 而不是抛异常")
    check(enc.encrypt("") is None, "加密空串返回 None")

    print("\n=== 3) 哪些记录该加密 ===")
    sensitive_text = ClipboardEntry(content_type="text", content=ID_CARD_TEXT,
                                    preview="x", is_sensitive=1)
    plain_entry = ClipboardEntry(content_type="text", content=PLAIN_TEXT,
                                 preview="x", is_sensitive=0)
    image_entry = ClipboardEntry(content_type="image", content=r"C:\pic.png",
                                 preview="[图片]", is_sensitive=1)
    file_entry = ClipboardEntry(content_type="file", content=r"C:\a.txt",
                                preview="[文件]", is_sensitive=1)

    check(enc.should_encrypt(sensitive_text) is True, "敏感文本该加密")
    check(enc.should_encrypt(plain_entry) is False, "非敏感记录不加密")
    check(enc.should_encrypt(image_entry) is False,
          "**图片不加密**（content 是磁盘路径，加密了就找不到附件）")
    check(enc.should_encrypt(file_entry) is False, "**文件不加密**（同上）")
    check(enc.protect_entry(image_entry)[0] == r"C:\pic.png",
          "图片记录的 content 原样保留")

    # ==================================================================
    print("\n=== 4) 端到端：库里是密文，读出来是明文 ===")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_enc_")
    print("       数据目录：%s" % tmp)
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=False)
    try:
        kernel.simulate_capture(ID_CARD_TEXT)
        kernel.simulate_capture(PLAIN_TEXT)
        rows = kernel.repo.get_history(limit=10)
        secret = next(r for r in rows if "110101199003074567" in (r.content or ""))
        plain = next(r for r in rows if "普通" in (r.content or ""))

        check(secret.is_sensitive == 1, "身份证记录被识别为敏感")
        check(plain.is_sensitive == 0, "普通记录没有被误判")

        db_path = os.path.join(tmp, "clipboardpro.db")
        stored = raw_content(db_path, secret.id)
        check(enc.is_encrypted(stored), "**库里存的是密文**：%s" % stored[:14])
        check("110101199003074567" not in stored, "**明文不在库里**")
        check(raw_content(db_path, plain.id) == PLAIN_TEXT, "普通记录仍是明文")

        check("110101199003074567" in kernel.repo.find_by_id(secret.id).content,
              "find_by_id 透明解密")
        check("110101199003074567" in kernel.repo.get_entry(secret.id).content,
              "get_entry 透明解密")
        listed = [r for r in kernel.repo.get_history(limit=10) if r.id == secret.id]
        check(listed and "110101199003074567" in listed[0].content,
              "get_history 也解密了（每条读路径都不能漏）")

        # 加密不影响去重：content_hash 基于明文
        kernel.simulate_capture(ID_CARD_TEXT)
        check(kernel.repo.count() == 2,
              "重复的敏感内容仍然按明文合并（没多出一条）")

        # ==============================================================
        print("\n=== 5) 加密之后还要搜得到 ===")
        found = kernel.search_history("110101199003074567")
        check(found["data"]["total"] >= 1,
              "**搜身份证号能搜到**（密文靠内存补扫）")
        check(any("110101199003074567" in (i["content"] or "")
                  for i in found["data"]["items"]),
              "搜到的确实是那条记录")
        check(kernel.search_history("完全不存在的东西")["data"]["total"] == 0,
              "不存在的关键词返回 0 条（没把无关记录也捞出来）")
        check(kernel.search_history("普通")["data"]["total"] >= 1,
              "明文记录照常搜得到")

        # ==============================================================
        print("\n=== 6) 敏感标签 -> 自动加密 ===")
        kernel.simulate_capture("一段本来很普通的话")
        candidate = kernel.repo.get_history(limit=1)[0]
        check(candidate.is_sensitive == 0, "普通内容初始不敏感")

        kernel.add_tag(candidate.id, "sensitive")
        tagged = kernel.repo.find_by_id(candidate.id)
        check(tagged.is_sensitive == 1, "**打上 sensitive 标签后自动标记为敏感**")
        check(enc.is_encrypted(raw_content(db_path, candidate.id)),
              "而且真的加密落盘了")

        kernel.remove_tag(candidate.id, "sensitive")
        reverted = kernel.repo.find_by_id(candidate.id)
        check(reverted.is_sensitive == 0, "去掉标签后取消敏感")
        check(raw_content(db_path, candidate.id) == "一段本来很普通的话",
              "内容也解回明文了")

        # 关键：内容本身仍是敏感时，删标签不能撤掉保护
        kernel.add_tag(secret.id, "sensitive")
        kernel.remove_tag(secret.id, "sensitive")
        kept = kernel.repo.find_by_id(secret.id)
        check(kept.is_sensitive == 1,
              "**内容本身是身份证号时，删掉标签仍保持加密**（否则删个标签就明文了）")
        check(enc.is_encrypted(raw_content(db_path, secret.id)), "库里仍是密文")

        # ==============================================================
        print("\n=== 7) 标记与落盘形态的一致性 ===")
        check(kernel.mark_sensitive(plain.id, True)["ok"], "手动标记敏感")
        check(enc.is_encrypted(raw_content(db_path, plain.id)),
              "手动标记后立即加密落盘")
        check(kernel.mark_sensitive(plain.id, False)["ok"], "手动取消敏感")
        check(raw_content(db_path, plain.id) == PLAIN_TEXT, "取消后解回明文")

        stats = kernel.repo.alignment_stats()
        check(stats["missingEncryption"] == [] and stats["staleEncryption"] == [],
              "**没有标记与形态不一致的记录**：%s" % stats)
        report = kernel.realign_encryption()["data"]
        check(report["fixed"] == 0, "已经一致时自愈不做无用功")

        status = kernel.get_encryption_status()["data"]
        check(status["available"] and status["aligned"], "加密状态报告：已对齐")
        check(status["encryptableTypes"] == list(enc.ENCRYPTABLE_TYPES),
              "报告里列出了可加密类型")

        # ==============================================================
        print("\n=== 8) 解不开时的占位说明 ===")
        broken = ClipboardEntry(content_type="text",
                                content=enc.ENCRYPT_PREFIX + "A" * 120,
                                preview="x", is_sensitive=1)
        broken.id = 999
        enc.unprotect_entry(broken)
        check("无法解密" in (broken.content or ""),
              "解不开时给出明确说明而不是留 base64：%s" % broken.content)

    finally:
        kernel.shutdown()

    # ==================================================================
    print("\n=== 9) 隐私规则扩展（掩码位数 / 自定义规则 / 邮箱域名）===")
    service = pv.PrivacyService(enabled=True, kinds=pv.ALL_KINDS)
    service.add_custom_rule(r"AB\d+CD")

    service.configure(mask_prefix=0, mask_suffix=0)
    check(service.scan("AB1234567890CD")[0].masked == "*" * 14,
          "前后 0 位可见 → 全打码")
    service.configure(mask_prefix=2, mask_suffix=2)
    check(service.scan("AB1234567890CD")[0].masked == "AB**********CD",
          "前后 2 位可见：%s" % service.scan("AB1234567890CD")[0].masked)
    service.configure(mask_prefix=20, mask_suffix=20)
    check("*" * 14 == service.scan("AB1234567890CD")[0].masked,
          "**前后可见位数盖住整串时退化为全打码**（否则等于没脱敏）")
    check(service.configure(mask_prefix="坏值")["maskPrefix"] == 3,
          "非法位数退回默认值 3")

    service.configure(mask_email_domain=False)
    check(service.scan("someone@example.com")[0].masked == "s******@example.com",
          "默认保留邮箱域名")
    service.configure(mask_email_domain=True)
    masked = service.scan("someone@example.com")[0].masked
    check("example" not in masked and masked.endswith(".com"),
          "开启后连域名一起打码：%s" % masked)

    service2 = pv.PrivacyService(enabled=True)
    loaded = service2.load_custom_rules("AB\\d+CD\n# 注释行\n\n[(")
    check(loaded == 1, "**坏正则被跳过、注释与空行被忽略**（加载了 %d 条）" % loaded)
    check(len(service2.scan("AB12345CD")) == 1, "好规则仍然生效")

    check(pv.KIND_URL in pv.ALL_KINDS and pv.KIND_PASSWORD in pv.ALL_KINDS,
          "可选类型里有网址与密码")
    check(pv.KIND_URL not in pv.DEFAULT_KINDS,
          "**网址默认不开**（剪贴板里网址太多，全脱敏就没法用了）")
    check(set(pv.DEFAULT_KINDS) == {pv.KIND_ID_CARD, pv.KIND_PHONE,
                                    pv.KIND_EMAIL, pv.KIND_SECRET},
          "默认开启的仍是身份证/手机号/邮箱/密钥四类")

    print("\n" + "=" * 72)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("\n失败清单：")
        for item in FAILED:
            print("  - %s" % item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
