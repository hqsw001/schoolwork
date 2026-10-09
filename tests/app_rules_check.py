# -*- coding: utf-8 -*-
"""应用级规则引擎回归（差距清单批次 10）

对应 TieZ 的 ``AppCleanupPolicy``（忽略采集 / 自动清洗）与全局清洗规则。

本套件盯住七个容易写错的地方：

1. **四种匹配写法都要认**：完整路径、exe 名、``/正则/``、MSIX AUMID。
2. **忽略采集要在处理之前生效。** 放在管线末尾等于白白解码图片、算指纹，
   最后才把结果丢掉。
3. **``ignore`` 不写类型 = 全部忽略；``clean`` 不写类型 = 不匹配。**
   两者语义相反，混淆了就会"以为忽略了其实没忽略"。
4. **清洗规则绝不能把内容清空。** 写了 ``clean`` 却没有任何有效规则时，
   正确的做法是**不干预**——静默丢数据比不生效糟糕得多。
5. **坏正则只跳过自己那一条。** 一条写错不能让整块采集失效。
6. **图片/文件类不能被文本正则清洗。** 它们的 content 是磁盘路径，
   替换一下就把路径改坏了。
7. **规则数据坏掉不能炸。** 配置里是 JSON，用户可能手改坏。

用法：python -m tests.app_rules_check
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.app.kernel import Kernel  # noqa: E402
from app.constants import ERR_INVALID_ARG, ERR_NOT_FOUND  # noqa: E402
from app.services import app_rules as ar  # noqa: E402

PASSED = []
FAILED = []


def check(condition, message):
    if condition:
        PASSED.append(message)
        print("  [OK]   %s" % message)
    else:
        FAILED.append(message)
        print("  [FAIL] %s" % message)


def main():
    # ==================================================================
    print("=== 1) 清洗规则解析 ===")
    rules = ar.parse_cleanup_rules(
        "\n".join([
            "# 这是注释",
            "",
            r"^\s*\d+\t  =>",
            "旧词 => 新词",
            "没有箭头的行",
            "[(",                      # 坏正则
            r"a => b\nc",              # 替换值里的转义
        ]))
    check(len(rules) == 3, "跳过注释/空行/无箭头/坏正则，得到 %d 条" % len(rules))
    check(ar.apply_cleanup_rules("12\thello", rules) == "hello",
          "去行号规则生效")
    check(ar.apply_cleanup_rules("a", rules) == "b\nc",
          r"替换值里的 \n 被还原成真正的换行：%r" % ar.apply_cleanup_rules("a", rules))
    check(ar.apply_cleanup_rules("原样", []) == "原样", "没有规则时原样返回")
    check(ar.parse_cleanup_rules(None) == [], "None 返回空列表")
    check(ar.parse_cleanup_rules("  =>  ") == [], "只有箭头没有正则的被跳过")

    # ==================================================================
    print("\n=== 2) 应用匹配的四种写法 ===")
    check(ar.executable_name_matches(r"C:\App\a.exe", "c:/app/A.EXE") is True,
          "完整路径比 exe 名（大小写不敏感）")
    check(ar.executable_name_matches("a.exe", r"C:\b\a.exe") is True,
          "exe 名比完整路径")
    check(ar.executable_name_matches("a.exe", "b.exe") is False, "不同 exe 不匹配")
    check(ar.executable_name_matches("记事本", "记事本") is False,
          "不含 .exe 的值不当作文件名")
    check(ar.executable_name_matches("", "a.exe") is False, "空值不匹配")

    aumid = "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"
    path = (r"C:\Program Files\WindowsApps"
            r"\Microsoft.WindowsNotepad_1.0_x64__8wekyb3d8bbwe\Notepad.exe")
    check(ar.app_id_matches_process_path(aumid, path) is True, "MSIX AUMID 匹配进程路径")
    check(ar.app_id_matches_process_path("Notepad", path) is False,
          "没有 ! 的不当 AUMID 处理")
    check(ar.app_id_matches_process_path(aumid, r"C:\x\Other.exe") is False,
          "包名对不上的不匹配")

    policies = [
        {"id": "1", "action": "ignore", "appPath": r"C:\Tools\keepass.exe"},
        {"id": "2", "action": "ignore", "appName": "/银行|支付/"},
        {"id": "3", "action": "clean", "appName": "Code.exe",
         "cleanupRules": r"^\s*\d+\t  =>"},
        {"id": "4", "action": "ignore", "appPath": aumid, "contentTypes": ["image"]},
    ]
    engine = ar.AppRuleEngine(ar.dump_app_cleanup_policies(policies))
    check(engine.count() == 4, "加载 %d 条策略" % engine.count())

    cases = [
        ("keepass.exe", r"C:\Tools\KEEPASS.EXE", "text", "ignore", "1"),
        ("招商银行客户端", r"C:\Bank\cmb.exe", "text", "ignore", "2"),
        ("Code.exe", r"C:\MS\Code.exe", "text", "clean", "3"),
        ("记事本", r"C:\WindowsApps\Microsoft.WindowsNotepad_1.0_x64__8wekyb3d8bbwe\N.exe",
         "image", "ignore", "4"),
        ("微信", r"C:\WeChat.exe", "text", None, None),
    ]
    for app, path, kind, want_action, want_id in cases:
        action, _text, policy = engine.evaluate(app, path, kind, "x")
        got_id = (policy or {}).get("id")
        check(action == want_action and got_id == want_id,
              "%-14s %-6s → action=%s policy=%s" % (app, kind, action, got_id))

    # ==================================================================
    print("\n=== 3) ignore 与 clean 的类型语义相反 ===")
    engine2 = ar.AppRuleEngine(
        '[{"action":"ignore","appName":"X"},{"action":"clean","appName":"Y"}]')
    check(engine2.evaluate("X", "", "text", "t")[0] == "ignore",
          "**ignore 不写类型 = 文本也忽略**")
    check(engine2.evaluate("X", "", "image", "t")[0] == "ignore",
          "**ignore 不写类型 = 图片也忽略**")
    check(engine2.evaluate("Y", "", "text", "t")[0] is None,
          "clean 不写类型且无规则 → 不干预")

    engine3 = ar.AppRuleEngine(
        '[{"action":"clean","appName":"Z","contentTypes":["code"],'
        '"cleanupRules":"a => b"}]')
    check(engine3.evaluate("Z", "", "code", "a")[0] == "clean", "类型对得上就清洗")
    check(engine3.evaluate("Z", "", "text", "a")[0] is None,
          "**clean 写了类型时，类型对不上不匹配**")

    # ==================================================================
    print("\n=== 4) 坏数据不能炸 ===")
    check(ar.parse_app_cleanup_policies("{不是 json") == [], "非 JSON 返回空")
    check(ar.parse_app_cleanup_policies('{"a":1}') == [], "非数组返回空")
    check(ar.parse_app_cleanup_policies('[{"action":"ignore"}]') == [],
          "没有匹配目标的策略被过滤")
    check(len(ar.parse_app_cleanup_policies(
        '["x", 1, {"action":"ignore","appName":"A"}]')) == 1,
        "非字典项被跳过，字典项保留")
    check(ar.parse_app_cleanup_policies(None) == [], "None 返回空")
    bad_action = ar.normalize_policy({"appName": "A", "action": "乱写"})
    check(bad_action["action"] == ar.ACTION_CLEAN, "非法动作退回默认 clean")

    # ==================================================================
    print("\n=== 5) 端到端：忽略采集 ===")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_apprules_")
    print("       数据目录：%s" % tmp)
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=False)
    try:
        result = kernel.set_app_cleanup_policies([{
            "id": "r1", "enabled": True, "appName": "KeePass.exe",
            "appPath": r"C:\Tools\KeePass.exe", "action": "ignore",
            "contentTypes": ["text"], "cleanupRules": "",
        }])
        check(result["ok"] and result["data"]["count"] == 1, "保存 1 条忽略规则")
        listed = kernel.get_app_cleanup_policies()["data"]["items"]
        check(listed[0].get("summary") == "忽略 KeePass.exe 的文本",
              "摘要可读：%s" % listed[0].get("summary"))

        kernel.simulate_capture("密码管理器里的密码", source_name="KeePass.exe")
        check(kernel.repo.count() == 0,
              "**来自被忽略应用的内容根本没入库**（条数 %d）" % kernel.repo.count())
        kernel.simulate_capture("微信里的普通内容", source_name="WeChat.exe")
        check(kernel.repo.count() == 1, "其他应用照常采集")

        # 禁用的规则不生效
        disabled = kernel.get_app_cleanup_policies()["data"]["items"]
        for policy in disabled:
            policy.pop("summary", None)
        disabled[0]["enabled"] = False
        kernel.set_app_cleanup_policies(disabled)
        kernel.simulate_capture("禁用规则后应该能入库", source_name="KeePass.exe")
        check(kernel.repo.count() == 2, "**禁用后规则不再生效**")

        # ==============================================================
        print("\n=== 6) 端到端：自动清洗 ===")
        kernel.set_app_cleanup_policies([{
            "id": "r2", "enabled": True, "appName": "Code.exe",
            "action": "clean", "contentTypes": ["code", "text"],
            "cleanupRules": r"^\s*\d+\t  =>",
        }])
        kernel.simulate_capture("1\tdef hello():\n2\t    return 1",
                                source_name="Code.exe")
        row = kernel.repo.get_history(limit=1)[0]
        check("def hello" in row.content and "1\t" not in row.content,
              "**来自 Code.exe 的内容被自动去掉行号**：%r" % row.content)

        # 图片不能被文本正则清洗（content 是磁盘路径）
        kernel.set_app_cleanup_policies([{
            "id": "r3", "enabled": True, "appName": "WeChat.exe",
            "action": "clean", "contentTypes": ["image", "file"],
            "cleanupRules": r"\d  =>X",
        }])
        engine4 = kernel.app_rules
        action, text, _p = engine4.evaluate("WeChat.exe", "", "image", r"C:\a\1.png")
        check(action is None and text == r"C:\a\1.png",
              "**引擎拒绝清洗图片路径**，原样返回：%r" % text)
        check(engine4.evaluate("WeChat.exe", "", "file", r"C:\a\1.txt")[0] is None,
              "文件类同样不被清洗")
        check(engine4.evaluate("WeChat.exe", "", "text", "1")[0] is None,
              "这条策略只写了图片/文件，文本类型不匹配它")
        text_engine = ar.AppRuleEngine(
            '[{"action":"clean","appName":"WeChat.exe","contentTypes":["text"],'
            r'"cleanupRules":"\\d  =>X"}]')
        check(text_engine.evaluate("WeChat.exe", "", "text", "1")[0] == "clean",
              "**换成文本策略后照常清洗**（闸门只挡类型，不挡功能）")
        check("image" not in ar.TEXT_CONTENT_TYPES,
              "**图片不在可清洗类型里**")

        # ==============================================================
        print("\n=== 7) 试跑与一键加规则 ===")
        kernel.set_app_cleanup_policies([{
            "id": "r4", "enabled": True, "appName": "Code.exe",
            "action": "clean", "contentTypes": ["code"],
            "cleanupRules": r"^\s*\d+\t  =>",
        }])
        probe = kernel.test_app_rule("Code.exe", None, "code", "1\tx = 1")["data"]
        check(probe["action"] == "clean" and probe["changed"] is True,
              "试跑报告 action=clean changed=True")
        check(probe["after"] == "x = 1", "试跑给出清洗后的内容：%r" % probe["after"])
        miss = kernel.test_app_rule("WeChat.exe", None, "text", "x")["data"]
        check(miss["matched"] is False and miss["action"] == "none",
              "没命中时 action=none")

        kernel.simulate_capture("来自某个工具的内容", source_name="SecretTool.exe")
        row2 = kernel.repo.get_history(limit=1)[0]
        added = kernel.add_app_rule_for_entry(row2.id, "ignore")
        check(added["ok"], "从记录一键加规则")
        again = kernel.add_app_rule_for_entry(row2.id, "ignore")
        check(not again["ok"] and again["error"]["code"] == ERR_INVALID_ARG,
              "同一个应用不会重复加规则")

        # ==============================================================
        print("\n=== 8) 删除与全局规则 ===")
        before = kernel.get_app_cleanup_policies()["data"]["count"]
        removed = kernel.delete_app_cleanup_policy(0)
        check(removed["ok"]
              and kernel.get_app_cleanup_policies()["data"]["count"] == before - 1,
              "删除一条规则（%d → %d）"
              % (before, kernel.get_app_cleanup_policies()["data"]["count"]))
        out_of_range = kernel.delete_app_cleanup_policy(99)
        check(not out_of_range["ok"] and out_of_range["error"]["code"] == ERR_NOT_FOUND,
              "越界删除返回 %s" % ERR_NOT_FOUND)

        saved = kernel.set_cleanup_rules("\\s+$  =>\n# 注释行")
        check(saved["ok"] and saved["data"]["count"] == 1,
              "全局清洗规则保存，%d 条生效" % saved["data"]["count"])
        fetched = kernel.get_global_cleanup_rules()["data"]
        check(fetched["count"] == 1 and "# 注释行" in fetched["raw"],
              "读回来的原文保留了注释")
        kernel.simulate_capture("尾部有空格   ")
        check(kernel.repo.get_history(limit=1)[0].content == "尾部有空格",
              "**全局规则在采集时真的生效了**")

        empty = kernel.set_app_cleanup_policies("不是列表")
        check(not empty["ok"] and empty["error"]["code"] == ERR_INVALID_ARG,
              "非列表被拒绝（%s）" % ERR_INVALID_ARG)
        no_target = kernel.set_app_cleanup_policies(
            [{"id": "x", "action": "ignore"}])
        check(not no_target["ok"],
              "没有匹配目标的规则被拒绝，而不是存进去当摆设")

    finally:
        kernel.shutdown()

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
