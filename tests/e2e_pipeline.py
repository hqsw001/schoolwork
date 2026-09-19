# -*- coding: utf-8 -*-
"""端到端链路测试：复制 → 入库 → 列表 → 检索 → 置顶/标签 → 去重 → 裁剪 → 粘贴写回

本测试不需要界面，也不需要真实剪贴板，直接驱动 Kernel 的公开接口，
验证的是"界面之下"的完整业务链路。真实剪贴板与模拟粘贴由
``tests/manual_paste_check.py`` 在真机上单独验证。

用法：
    python -m tests.e2e_pipeline
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.app.kernel import Kernel  # noqa: E402
from app.constants import (  # noqa: E402
    K_DEDUPLICATE,
    K_PERSISTENT,
    K_PERSISTENT_LIMIT,
    K_PERSISTENT_LIMIT_ENABLED,
)
from app.services import content_utils  # noqa: E402

PASSED = []
FAILED = []


def check(condition, message):
    if condition:
        PASSED.append(message)
        print("  [OK]   %s" % message)
    else:
        FAILED.append(message)
        print("  [FAIL] %s" % message)


def section(title):
    print("\n=== %s ===" % title)


def ok_data(result, label):
    """命令接口统一返回 {ok, data, error, timestamp}；这里顺带断言调用成功。"""
    if not result.get("ok"):
        error = result.get("error") or {}
        raise AssertionError("%s 调用失败：%s" % (label, error.get("message")))
    return result["data"]


# ---------------------------------------------------------------------------
def test_type_detection():
    section("UC-02 内容格式识别")
    check(content_utils.detect_text_type("https://github.com/jimuzhe/tiez-clipboard") == "url",
          "http 网址判定为 url")
    check(content_utils.detect_text_type("www.baidu.com") == "url", "www. 开头判定为 url")
    check(content_utils.detect_text_type("这是一段普通的中文文本") == "text",
          "普通文本判定为 text")
    check(content_utils.detect_text_type(
        "import os\n\n\ndef main():\n    return os.getcwd()\n") == "code",
        "Python 片段判定为 code")
    check(content_utils.detect_text_type('{"name": "剪贴板Pro", "version": 1}') == "code",
          "JSON 片段判定为 code")
    check(content_utils.detect_text_type("请把 return 的结果发我，谢谢。") == "text",
          "含关键字的自然语言不会被误判为代码（业务规则 R1）")
    check(content_utils.looks_like_url("不是网址 这里有空格") is False,
          "含空格的文本不会被误判为网址")


def test_normalization_and_hash():
    section("F6-1 文本指纹与规范化")
    a = "第一行\r\n第二行\r\n"
    b = "第一行\n第二行"
    check(content_utils.text_hash(a) == content_utils.text_hash(b),
          "换行符不同但内容相同的文本指纹一致（UC-06 第 4 步）")
    check(content_utils.text_hash("abc") != content_utils.text_hash("abd"),
          "内容不同指纹不同")
    # 演示 UC-06 业务规则 R1：不能只看前 N 个字符
    long_a = "A" * 500 + "结尾不同之一"
    long_b = "A" * 500 + "结尾不同之二"
    check(content_utils.text_hash(long_a) != content_utils.text_hash(long_b),
          "前半段相同后半段不同时指纹不同（禁止只比前缀）")


def test_privacy():
    section("F9-1 / F9-2 敏感识别与脱敏")
    from app.services.privacy import PrivacyService
    privacy = PrivacyService(enabled=True)
    check(privacy.is_sensitive("我的手机号是13812345678"), "识别出手机号")
    check(privacy.is_sensitive("邮箱 test.user@example.com"), "识别出邮箱")
    check(privacy.is_sensitive("身份证 110101199003078515"), "识别出身份证号")
    check(privacy.is_sensitive("token=ghp_AbCdEfGhIjKlMnOpQrStUvWx"), "识别出密钥类字符串")
    check(not privacy.is_sensitive("这是一段完全正常的说明文字"), "正常文本不误报")
    masked = privacy.mask_preview("联系我：13812345678 或 test.user@example.com")
    check("13812345678" not in masked, "脱敏后手机号明文不再出现")
    check("test.user@example.com" not in masked, "脱敏后邮箱明文不再出现")
    check("138****5678" in masked, "手机号按「保留前 3 后 4」规则脱敏")


def test_pipeline_end_to_end():
    section("UC-01 / UC-03 / UC-06 复制 → 入库 → 去重合并")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_e2e_")
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=False)          # 不注册真实监听，避免影响本机剪贴板

    # ---- 首次采集
    result = ok_data(kernel.simulate_capture("剪贴板Pro 端到端测试内容", source_name="记事本"),
                     "simulate_capture")
    check(result["outcome"] == "inserted", "首次采集结果为 inserted")
    entry_id = result["entry"]["id"]
    check(entry_id > 0, "记录获得自增 id = %s" % entry_id)
    check(result["contentType"] == "text", "类型识别为 text")

    # ---- 重复采集：不新增行，只刷新时间与次数（UC-06）
    before = ok_data(kernel.get_count(), "get_count")["count"]
    result2 = ok_data(kernel.simulate_capture("剪贴板Pro 端到端测试内容",
                                              source_name="Chrome 浏览器"),
                      "simulate_capture(dup)")
    check(result2["outcome"] == "merged", "重复内容判定为 merged")
    check(ok_data(kernel.get_count(), "get_count")["count"] == before,
          "重复内容不新增记录行")
    merged = result2["entry"]
    check(merged["id"] == entry_id, "合并刷新命中同一条记录")
    check(merged["useCount"] == 1, "使用次数累计为 1")
    check(merged["sourceApp"] == "Chrome 浏览器", "来源应用更新为本次来源")

    # ---- 换行符不同的相同内容也应合并
    kernel.simulate_capture("多行内容\r\n第二行", source_name="A")
    result3 = ok_data(kernel.simulate_capture("多行内容\n第二行", source_name="A"),
                      "simulate_capture(换行符)")
    check(result3["outcome"] == "merged", "换行符不同的相同文本被识别为重复")

    # ---- 置顶与标签
    pin = kernel.toggle_pin(entry_id)
    check(pin["ok"] and pin["data"]["isPinned"], "置顶成功")
    tag = kernel.add_tag(entry_id, "重要")
    check(tag["ok"] and tag["data"]["tags"] == ["重要"], "添加标签成功")

    # ---- 列表排序：置顶优先
    history = ok_data(kernel.get_history(limit=10), "get_history")["items"]
    check(history[0]["id"] == entry_id, "置顶记录排在列表最前（UC-10 业务规则 R2）")

    # ---- 检索
    found = ok_data(kernel.search_history("端到端"), "search_history")["items"]
    check(len(found) == 1 and found[0]["id"] == entry_id, "按正文检索命中 1 条")
    found = ok_data(kernel.search_history("重要", tag_only=True), "search(tagOnly)")["items"]
    check(len(found) == 1, "仅按标签检索命中 1 条")
    found = ok_data(kernel.search_history("Chrome"), "search(source)")["items"]
    check(len(found) == 1, "按来源应用检索命中 1 条")
    found = ok_data(kernel.search_history("%"), "search(%)")["items"]
    check(len(found) == 0, "通配符 % 被转义，不会匹配到全部记录")

    # ---- 敏感内容入库后按脱敏显示
    kernel.simulate_capture("我的手机号是13812345678请你记一下", source_name="微信")
    sensitive_items = ok_data(kernel.search_history("手机号"), "search(敏感)")["items"]
    check(len(sensitive_items) == 1, "敏感记录可以入库")
    item = sensitive_items[0]
    check(item["isSensitive"] is True, "敏感记录带上了敏感标记")
    check("13812345678" not in item["preview"], "敏感记录预览按脱敏显示（F9-2）")
    full = ok_data(kernel.get_entry(item["id"]), "get_entry")
    check("13812345678" in full["contentFull"],
          "主动查看时仍能看到原文（UC-17 业务规则 R3）")

    # ---- 编辑记录并重算指纹
    edited = kernel.update_entry_content(entry_id, "被编辑过的内容")
    check(edited["ok"] and edited["data"]["content"] == "被编辑过的内容", "编辑记录成功")
    check(edited["data"]["preview"] == "被编辑过的内容", "编辑后预览同步更新")
    again = ok_data(kernel.simulate_capture("被编辑过的内容", source_name="B"),
                    "simulate_capture(编辑后)")
    check(again["outcome"] == "merged", "编辑后按新指纹判重生效（UC-12 业务规则 R2）")

    kernel.shutdown()
    return tmp


def test_persistence_restore():
    section("F2-4 启动时历史恢复")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_e2e_restore_")
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=False)
    kernel.simulate_capture("重启后应该还在的内容", source_name="记事本")
    first_id = ok_data(kernel.get_history(limit=1), "get_history")["items"][0]["id"]
    kernel.toggle_pin(first_id)
    kernel.add_tag(first_id, "保留")
    kernel.shutdown()

    kernel2 = Kernel(data_dir=tmp)
    kernel2.start(enable_listener=False)
    items = ok_data(kernel2.get_history(limit=10), "get_history(重启后)")["items"]
    check(len(items) == 1, "重启后历史记录被恢复")
    check(items[0]["content"] == "重启后应该还在的内容", "内容完整恢复")
    check(items[0]["isPinned"] is True, "置顶状态被持久化（UC-10 业务规则 R1）")
    check(items[0]["tags"] == ["保留"], "标签关联被持久化")
    kernel2.shutdown()


def test_echo_suppression():
    section("F1-6 / UC-09 自粘贴回声抑制")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_e2e_echo_")
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=False)
    kernel.simulate_capture("回声抑制测试内容", source_name="记事本")
    entry = ok_data(kernel.get_history(limit=1), "get_history")["items"][0]

    # 模拟"本程序把这条写回剪贴板"，再模拟读回来
    fingerprint = content_utils.text_hash(entry["content"])
    kernel.dedup.mark_paste(
        fingerprint=fingerprint, kind="text", content_type="text",
        raw_text=entry["content"],
        normalized_text=content_utils.normalize_for_compare(entry["content"]),
        entry_id=entry["id"],
    )
    echoed = ok_data(kernel.simulate_capture(entry["content"], source_name="剪贴板Pro"),
                     "simulate_capture(回声)")
    check(echoed["outcome"] == "echo", "10 秒窗口内指纹一致的采集被判定为回声并丢弃")
    check(ok_data(kernel.get_count(), "get_count")["count"] == 1, "回声没有产生新记录")

    # 标记已被消费：同样内容这次应正常合并（不会一直被抑制）
    after = ok_data(kernel.simulate_capture(entry["content"], source_name="记事本"),
                    "simulate_capture(标记已消费)")
    check(after["outcome"] == "merged", "标记被消费后，用户真正复制同样内容仍能正常处理")

    # 超时后不再抑制
    kernel.dedup.mark_paste(fingerprint=fingerprint, kind="text", content_type="text",
                            raw_text=entry["content"])
    kernel.dedup.window_ms = -1        # 把窗口设成负数，等价于"已超时"
    expired = ok_data(kernel.simulate_capture(entry["content"], source_name="记事本"),
                      "simulate_capture(超时)")
    check(expired["outcome"] == "merged", "超出 10 秒窗口后不再判定为回声")
    kernel.shutdown()


def test_capacity():
    section("F3 / UC-04 / UC-05 容量上限与裁剪")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_e2e_cap_")
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=False)
    kernel.settings.set(K_PERSISTENT_LIMIT_ENABLED, True)
    kernel.settings.set(K_PERSISTENT_LIMIT, 10)   # 最小值，便于测试
    kernel._apply_setting_side_effects(K_PERSISTENT_LIMIT, 10)

    for index in range(14):
        kernel.simulate_capture("容量测试内容第 %02d 条" % index, source_name="测试")

    stats = ok_data(kernel.get_storage_usage(), "get_storage_usage")
    check(stats["countable"] <= 10, "可计数条数被裁剪到上限以内（当前 %d）" % stats["countable"])
    check(stats["totalCount"] == 10, "总条数等于上限")

    oldest = ok_data(kernel.get_history(limit=50), "get_history(裁剪后)")["items"][-1]
    check("第 00 条" not in oldest["content"], "最旧的记录已被裁剪（按时间从旧到新）")

    # 置顶与已打标签的记录必须豁免
    items = ok_data(kernel.get_history(limit=50), "get_history")["items"]
    victim = items[-1]
    kernel.toggle_pin(victim["id"])
    protected_id = victim["id"]
    kernel.simulate_capture("再来一条触发裁剪的内容", source_name="测试")
    surviving_ids = [e["id"] for e in
                     ok_data(kernel.get_history(limit=50), "get_history")["items"]]
    check(protected_id in surviving_ids, "置顶记录豁免自动裁剪（UC-05 业务规则 R2）")

    stats = ok_data(kernel.get_storage_usage(), "get_storage_usage")
    check(stats["protectedCount"] >= 1, "容量统计正确报告受保护条数")
    check(stats["limit"] == 10, "容量统计正确报告上限")

    # 上限校验
    bad = kernel.set_storage_limit(5, True)
    check(bad["ok"] is False, "上限小于 10 时被拒绝（UC-04 业务规则 R2）")
    kernel.shutdown()


def test_dedup_switch_and_session_mode():
    section("F6 去重开关 / UC-03 备选流 A1 非持久化模式")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_e2e_switch_")
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=False)

    kernel.settings.set(K_DEDUPLICATE, False)
    kernel.simulate_capture("关闭去重后的重复内容", source_name="A")
    kernel.simulate_capture("关闭去重后的重复内容", source_name="B")
    check(ok_data(kernel.get_count(), "get_count")["count"] == 2,
          "关闭去重后相同内容保留两条记录")

    kernel.settings.set(K_DEDUPLICATE, True)
    kernel.settings.set(K_PERSISTENT, False)
    before = ok_data(kernel.get_count(), "get_count")["count"]
    session = ok_data(kernel.simulate_capture("非持久化模式下的内容", source_name="A"),
                      "simulate_capture(会话模式)")
    check(session["outcome"] == "inserted", "非持久化模式下内容仍然进入会话列表")
    check(ok_data(kernel.get_count(), "get_count")["count"] == before,
          "非持久化模式不写数据库（UC-03 备选流 A1）")
    check(session["entry"]["id"] < 0, "会话内记录使用负数 id，与数据库记录明确区分")
    kernel.shutdown()


def test_clipboard_write():
    section("UC-09 写回系统剪贴板（真实剪贴板，会覆盖当前剪贴板内容）")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_e2e_write_")
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=False)
    kernel.simulate_capture("写回剪贴板的验证内容 ABC-123", source_name="测试")
    entry = ok_data(kernel.get_history(limit=1), "get_history")["items"][0]

    # 只写剪贴板、不模拟按键，避免测试时把内容粘到别的窗口里
    result = kernel.paste_entry(entry["id"], simulate=False)
    check(result["ok"], "paste_entry 调用成功")
    check(kernel.dedup.marker_count() == 1,
          "粘贴前写入了回声抑制标记（UC-09 业务规则 R1 的顺序要求）")

    from app.infrastructure.clipboard import clipboard_io
    data = clipboard_io.read_clipboard_data()
    check(data is not None and data.kind == "text", "能从剪贴板读回刚写入的内容")
    check(data.text == "写回剪贴板的验证内容 ABC-123", "剪贴板内容与写入值一致")
    kernel.dedup.clear_markers()
    kernel.shutdown()


def test_html_helper():
    section("CF_HTML 头部偏移（富文本粘贴）")
    from app.infrastructure.clipboard.clipboard_io import build_cf_html, extract_html_fragment
    fragment = "<b>中文加粗</b> 与英文混排 mixed"
    cf_html = build_cf_html(fragment)
    check(extract_html_fragment(cf_html) == fragment,
          "自建的 CF_HTML 能被自己的解析器正确还原（含中文多字节偏移）")


def main():
    test_type_detection()
    test_normalization_and_hash()
    test_privacy()
    test_html_helper()
    test_pipeline_end_to_end()
    test_persistence_restore()
    test_echo_suppression()
    test_capacity()
    test_dedup_switch_and_session_mode()
    test_clipboard_write()

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
