# -*- coding: utf-8 -*-
"""富文本完整链路回归（差距清单批次 5）

覆盖 TieZ ``services/clipboard/utils.rs`` 里那块富文本处理，对应四个真实问题：

1. **CF_HTML 偏移量是字节不是字符**，且 ``StartHTML/EndHTML``（整篇）比
   ``StartFragment/EndFragment``（片段）信息更全 —— 用整篇切才能保住
   ``<head>`` 里的 CSS。*（写这个套件时就先踩了一次：第一版解析把 CSS
   丢了，预览断言直接把它抓出来。）*
2. **WPS 会吃掉片段开头的 ``<``**，发出 ``table border=0 ...>``。
3. **Office 的 HTML 里 mso-* 样式与注释能占一多半**，但**不能无差别删
   ``<style>``** —— 用户自己的样式要留着。
4. **Excel 复制出来的还有 RTF / Biff 等私有格式**，只存 HTML 的话
   "复制 Excel → 粘回 Excel"会掉公式与格式。

⚠ 会短暂占用系统剪贴板。用法：
    python -m tests.rich_text_check
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win32clipboard  # noqa: E402

from app.infrastructure.clipboard import clipboard_io  # noqa: E402
from app.infrastructure.clipboard import rich_text as rt  # noqa: E402
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


def make_cf_html(fragment, with_style=True, full_offsets=True):
    """造一份 CF_HTML。full_offsets=False 时把整篇偏移写成越界值，
    逼解析器走"只有片段偏移"的分支。"""
    style = "<style>.a{color:red}</style>" if with_style else ""
    head = ('Version:1.0\r\nStartHTML:{:010d}\r\nEndHTML:{:010d}\r\n'
            'StartFragment:{:010d}\r\nEndFragment:{:010d}\r\n')
    base = len(head.format(0, 0, 0, 0).encode())
    prefix = "<html><head>%s</head><body><!--StartFragment-->" % style
    suffix = "<!--EndFragment--></body></html>"
    start_fragment = base + len(prefix.encode())
    end_fragment = start_fragment + len(fragment.encode())
    end_html = end_fragment + len(suffix.encode())
    header = head.format(
        base if full_offsets else 99999999,
        end_html if full_offsets else 99999999,
        start_fragment, end_fragment)
    return header + prefix + fragment + suffix


def main():
    # ==================================================================
    print("=== 1) WPS 吃掉 < 的修复 ===")
    check(rt.repair_html_fragment("table border=0><tr><td>x</td></tr></table>")
          .startswith("<table"),
          "裸 table 补回了 <")
    check(rt.repair_html_fragment("<b>ok</b>") == "<b>ok</b>",
          "正常 HTML 原样返回")
    check(rt.repair_html_fragment("就是一段普通文本") == "就是一段普通文本",
          "普通文本不会被误补")
    check(rt.repair_html_fragment("") == "", "空字符串不炸")

    # ==================================================================
    print("\n=== 2) CF_HTML 解析 ===")
    full = rt.parse_cf_html(make_cf_html("<b>加粗</b>"))
    check(full is not None and ".a{color:red}" in full,
          "整篇偏移时 **CSS 被保留**（这是粘回去样式不丢的关键）")
    check("加粗" in full, "正文也在")

    frag_only = rt.parse_cf_html(make_cf_html("<b>加粗</b>", full_offsets=False))
    check(frag_only is not None and "加粗" in frag_only,
          "只有片段偏移时能取到片段：%r" % (frag_only,))

    # 没有样式的空壳：应当只回片段，不要白包一层 <html><body>
    plain_doc = rt.parse_cf_html(make_cf_html("<b>加粗</b>", with_style=False))
    check(plain_doc is not None and "<html" not in plain_doc.lower(),
          "整篇只是空壳时返回干净的片段（%r）" % (plain_doc,))
    check(plain_doc is not None and "加粗" in plain_doc, "空壳情况下正文仍在")

    # 反过来：片段里自带的 style= 属性不该被当成"整篇带样式"
    inline = rt.parse_cf_html(make_cf_html('<b style="color:red">加粗</b>',
                                           with_style=False))
    check(inline is not None and "加粗" in inline,
          "片段内联样式不影响判定，正文取得到")

    raw16 = make_cf_html("<b>宽字符</b>").encode("utf-16-le")
    decoded = rt.parse_cf_html(raw16)
    check(decoded is not None and "宽字符" in decoded, "UTF-16LE 的 CF_HTML 能解析")

    no_header = rt.parse_cf_html("<p>没有头部</p>")
    check(no_header is not None and "没有头部" in no_header,
          "没有头部偏移时退回原文")

    check(rt.parse_cf_html(None) is None, "None 返回 None")
    check(rt.parse_cf_html("") is None, "空串返回 None")
    check(rt.parse_cf_html("   ") is None, "全空白返回 None")

    # ==================================================================
    print("\n=== 3) Office 噪声清洗 ===")
    noisy = ('<html><head>'
             '<style>/* Style Definitions */ mso-style-name:Normal;'
             'p.MsoNormal{mso-:x}</style>'
             '<style>.user-style{color:blue}</style>'
             '</head><body><p class=MsoNormal>正文</p></body></html>')
    clean = rt.strip_office_noise(noisy)
    check("mso-style-name" not in clean, "Office 样式块被删掉")
    check(".user-style{color:blue}" in clean, "**用户自己的样式保留**（不能无差别删 style）")
    check("正文" in clean, "正文内容保留")

    check("<table" in rt.strip_office_noise("<tr><td>1</td></tr>"),
          "裸 <tr> 自动补上 <table> 外壳")
    already = "<table><tr><td>1</td></tr></table>"
    check(rt.strip_office_noise(already).count("<table") == 1,
          "已有 table 不会被重复包一层")
    check(rt.strip_office_noise("") == "", "空字符串不炸")

    # ==================================================================
    print("\n=== 4) 预览截断 ===")
    short, truncated = rt.truncate_html_for_preview("<p>短内容</p>")
    check(short == "<p>短内容</p>" and truncated is False, "短内容不截断")

    rows = "".join("<tr><td>第%03d行</td></tr>" % i for i in range(300))
    table = "<table>" + rows + "</table>"
    cut, truncated = rt.truncate_html_for_preview(table)
    check(truncated is True, "超长表格被截断（原 %d 字符）" % len(table))
    check(cut.count("</tr>") == rt.HTML_PREVIEW_MAX_ROWS,
          "按行截断保留 %d 行" % rt.HTML_PREVIEW_MAX_ROWS)
    check(cut.rstrip().endswith("</table>"),
          "截断后补回了 </table>，预览里不会出现半个表格")

    long_text = "<p>" + ("很长的内容" * 2000) + "</p>"
    cut2, truncated2 = rt.truncate_html_for_preview(long_text)
    check(truncated2 is True, "超长文本被截断")
    check(len(cut2) > rt.HTML_PREVIEW_MAX_CHARS // 2,
          "**没有退化成只剩开头的 <p>**（截后 %d 字符）" % len(cut2))
    check("很长的内容" in cut2, "正文内容还在")

    # ==================================================================
    print("\n=== 5) 命名格式往返 ===")
    html = "<b>表格</b>"
    formats = [
        {"name": rt.RTF_FORMAT_NAME, "data": b"{\\rtf1\\ansi hello}"},
        {"name": "Biff12", "data": bytes(range(256)) * 8},   # 含 0x00 的二进制
    ]
    packed = rt.embed_named_formats(html, formats)
    check(len(packed) > len(html), "打包后变长（附加内容在注释里）")
    check(rt.has_markers(packed), "has_markers 认得出来")

    back_html, back_formats = rt.extract_named_formats(packed)
    check(back_html == html, "取回的 HTML 干净无标记")
    check(len(back_formats) == 2, "取回 2 个格式")
    check([f["name"] for f in back_formats] == [rt.RTF_FORMAT_NAME, "Biff12"],
          "名字与顺序都没变")
    check(back_formats[0]["data"] == formats[0]["data"], "RTF 数据一致")
    check(back_formats[1]["data"] == formats[1]["data"], "含 0x00 的二进制数据也一致")

    check(rt.embed_named_formats(html, []) == html, "没有格式时不加标记")
    check(rt.embed_named_formats(html, None) == html, "None 不炸")

    too_big = [{"name": "Huge", "data": b"x" * (rt.MAX_FORMAT_BYTES + 1)}]
    check(rt.embed_named_formats(html, too_big) == html, "超过单格式上限的被丢弃")

    many = [{"name": "F%d" % i, "data": b"y" * 10} for i in range(20)]
    _h, kept = rt.extract_named_formats(rt.embed_named_formats(html, many))
    check(len(kept) <= rt.MAX_FORMAT_COUNT,
          "格式数量被限制在 %d 个以内（实际 %d）" % (rt.MAX_FORMAT_COUNT, len(kept)))

    broken = '<p>x</p><!--CLIPBOARDPRO_RICH_FORMATS:这不是合法base64-->'
    broken_html, broken_formats = rt.extract_named_formats(broken)
    check(broken_formats == [] and broken_html == "<p>x</p>",
          "**坏掉的标记被忽略，不会让整条记录打不开**")

    # ==================================================================
    print("\n=== 6) 标记绝不能漏进预览 ===")
    big = rt.embed_named_formats(
        "<p>正文</p>", [{"name": rt.RTF_FORMAT_NAME, "data": b"Z" * 3000}])
    plain = content_utils.strip_html(big)
    check("CLIPBOARDPRO" not in plain, "strip_html 里没有标记名")
    check(len(plain) < 100, "strip_html 结果很短（%d 字符），没把 base64 当正文" % len(plain))
    check("正文" in plain, "正文被正确提取")
    check(len(rt.strip_markers(big)) < 50, "strip_markers 后只剩纯 HTML")
    check(rt.strip_markers(None) is None, "strip_markers(None) 不炸")

    # ==================================================================
    print("\n=== 7) 端到端：真实剪贴板 → 采集 → 粘回 ===")
    html_id = clipboard_io.html_format_id()
    if not html_id:
        check(False, "拿不到 HTML Format 的注册 id")
    else:
        rtf_name = rt.RTF_FORMAT_NAME
        rtf_payload = "{\\rtf1\\ansi\\deff0 表格内容}".encode("utf-8")
        # 手工摆一个"Excel 那种"剪贴板：HTML + RTF 并存
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(
                win32clipboard.CF_UNICODETEXT, "表格内容")
            win32clipboard.SetClipboardData(
                html_id, make_cf_html("<table><tr><td>表格内容</td></tr></table>"))
            rtf_fmt = win32clipboard.RegisterClipboardFormat(rtf_name)
            win32clipboard.SetClipboardData(rtf_fmt, rtf_payload)
        finally:
            win32clipboard.CloseClipboard()

        data = clipboard_io.read_clipboard_data(
            {"capture_rich_text": True, "capture_files": True,
             "source_app": "Excel", "logger": None})
        check(data is not None and data.kind == "rich_text",
              "识别为 rich_text（kind=%s）" % (getattr(data, "kind", None)))
        if data is not None:
            check(rt.has_markers(data.html or ""),
                  "采集时把 RTF 打包进了 HTML 注释")
            stored_html, stored_formats = rt.extract_named_formats(data.html or "")
            names = [f["name"] for f in stored_formats]
            check(rtf_name in names, "存下来的格式里有 %s：%s" % (rtf_name, names))
            rtf_saved = next((f for f in stored_formats if f["name"] == rtf_name), None)
            check(rtf_saved is not None and rtf_saved["data"] == rtf_payload,
                  "RTF 内容逐字节一致")
            check("表格内容" in content_utils.strip_html(data.html or ""),
                  "预览提纯后能看到正文")

            # 粘回去：RTF 必须重新出现在剪贴板上
            clipboard_io.write_text_html_and_formats(
                data.text or "", data.html or "")
            win32clipboard.OpenClipboard()
            try:
                formats_now = []
                fmt = 0
                while True:
                    fmt = win32clipboard.EnumClipboardFormats(fmt)
                    if not fmt:
                        break
                    try:
                        formats_now.append(win32clipboard.GetClipboardFormatName(fmt))
                    except Exception:  # noqa: BLE001
                        continue
                restored = None
                if rtf_name in formats_now:
                    restored = win32clipboard.GetClipboardData(
                        win32clipboard.RegisterClipboardFormat(rtf_name))
            finally:
                win32clipboard.CloseClipboard()
            check(rtf_name in formats_now,
                  "粘回后剪贴板上有 %s（%s）" % (rtf_name, formats_now))
            check(restored == rtf_payload,
                  "粘回的 RTF 与原内容一致（Excel→Excel 不会掉格式）")

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
