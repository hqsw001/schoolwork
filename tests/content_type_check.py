# -*- coding: utf-8 -*-
"""内容类型回归（差距清单批次 13 的"视频类型"部分）

TieZ 把视频单独列成一类。本项目里视频与文件在**存储形态上完全一样**
（都是换行分隔的路径列表），区别只在类型标记与界面呈现。

所以这里盯住的是"新增类型时最容易漏掉的东西"：

1. **类型表要同步。** `ALL_TYPES` / `TYPE_LABELS` 缺一项，
   筛选按钮就少一个、卡片角标就空白。
2. **凡是判断"文件类"的地方都要用 `is_file_type()`。**
   直接比 `"file"` 的话，视频记录就没法粘贴、没法"打开所在文件夹"。
3. **混合内容不算视频。** 一次复制里既有视频又有文档，按"文件"处理更合理。
4. **判定要认大小写、带查询串的链接、以及路径里的空白。**

用法：python -m tests.content_type_check
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.app.kernel import Kernel  # noqa: E402
from app.constants import (  # noqa: E402
    ALL_TYPES,
    FILE_TYPES,
    TEXT_TYPES,
    TYPE_FILE,
    TYPE_LABELS,
    TYPE_VIDEO,
    VIDEO_EXTENSIONS,
    is_file_type,
    is_text_type,
    looks_like_video,
)
from app.services import app_rules  # noqa: E402
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


class FakeFiles(object):
    """只带 files 字段的假 ClipboardData。"""

    def __init__(self, files):
        self.kind = "files"
        self.files = files
        self.text = None
        self.html = None
        self.image_bytes = None


def main():
    # ==================================================================
    print("=== 1) 类型表一致性 ===")
    check(TYPE_VIDEO in ALL_TYPES, "ALL_TYPES 里有 video")
    check(TYPE_VIDEO in TYPE_LABELS, "TYPE_LABELS 里有 video")
    check(TYPE_LABELS[TYPE_VIDEO] == "视频",
          "中文名是「视频」：%s" % TYPE_LABELS[TYPE_VIDEO])
    check(set(ALL_TYPES) == set(TYPE_LABELS),
          "**ALL_TYPES 与 TYPE_LABELS 完全一致**（少一项界面就少一个筛选）")
    check(len(set(ALL_TYPES)) == len(ALL_TYPES), "ALL_TYPES 没有重复项")
    check(".mp4" in VIDEO_EXTENSIONS and ".mkv" in VIDEO_EXTENSIONS,
          "常见视频扩展名都在（%d 个）" % len(VIDEO_EXTENSIONS))
    check(all(ext.startswith(".") and ext == ext.lower()
              for ext in VIDEO_EXTENSIONS), "扩展名都是小写且带点")

    print("\n=== 2) is_file_type / is_text_type ===")
    check(is_file_type(TYPE_FILE) is True, "file 算文件类")
    check(is_file_type(TYPE_VIDEO) is True, "**video 也算文件类**")
    check(is_file_type("text") is False, "text 不算文件类")
    check(is_file_type(None) is False, "None 不算文件类")
    check(TYPE_VIDEO in FILE_TYPES and TYPE_FILE in FILE_TYPES, "FILE_TYPES 含两者")
    check(TYPE_VIDEO not in TEXT_TYPES, "**视频不是文本类型**（不可编辑）")
    check(is_text_type(TYPE_VIDEO) is False, "is_text_type(video) 为 False")
    check(is_text_type("text") is True, "文本类型仍可编辑")

    # ==================================================================
    print("\n=== 3) 视频判定 ===")
    cases = [
        ([r"C:\media\a.mp4"], True, "单个 mp4"),
        ([r"C:\media\A.MKV"], True, "大写扩展名"),
        ([r"C:\a.mp4", r"D:\b.avi"], True, "多个视频"),
        ([r"C:\a.mp4", r"C:\b.txt"], False, "**混合内容不算视频**"),
        ([r"C:\docs\报告.pdf"], False, "普通文件不算"),
        ([], False, "空列表不算"),
        (None, False, "None 不算"),
        (["https://cdn.example.com/v.mp4?token=abc"], True, "带查询串的链接"),
        ([r"C:\a.mp4", "   "], True, "空白项被忽略"),
        ([r"C:\没有扩展名的文件"], False, "没有扩展名不算"),
        ([r"C:\a.MP4"], True, "扩展名大小写不敏感"),
    ]
    for paths, expected, label in cases:
        check(looks_like_video(paths) is expected,
              "%-18s -> %s" % (label, expected))

    # ==================================================================
    print("\n=== 4) 类型判定入口 ===")
    kind, content, html = content_utils.detect_content_type(
        FakeFiles([r"C:\media\电影.mp4"]))
    check(kind == TYPE_VIDEO, "采集一批视频路径 → video（实际 %s）" % kind)
    check(content == r"C:\media\电影.mp4", "正文就是路径列表")
    check(html is None, "视频没有 HTML")

    kind2, content2, _ = content_utils.detect_content_type(
        FakeFiles([r"C:\docs\报告.pdf"]))
    check(kind2 == TYPE_FILE, "普通文件仍是 file（实际 %s）" % kind2)

    kind3, _, _ = content_utils.detect_content_type(
        FakeFiles([r"C:\a.mp4", r"C:\b.txt"]))
    check(kind3 == TYPE_FILE, "**混合内容按 file 处理**（实际 %s）" % kind3)

    multi, multi_content, _ = content_utils.detect_content_type(
        FakeFiles([r"C:\a.mp4", r"D:\b.mkv"]))
    check(multi == TYPE_VIDEO and "\n" in multi_content,
          "多个视频用换行分隔存一条")

    # ==================================================================
    print("\n=== 5) 应用规则认这个类型 ===")
    check("video" in app_rules.DEFAULT_CONTENT_TYPES,
          "**应用规则的可选类型里有 video**")
    check(app_rules.TYPE_LABELS.get("video") == "视频",
          "规则摘要里显示「视频」而不是英文 key")

    # ==================================================================
    print("\n=== 6) 内核：视频记录能像文件一样用 ===")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_type_")
    print("       数据目录：%s" % tmp)
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=False)
    try:
        # 造一条视频记录（不走剪贴板，直接用管线入库）
        from app.domain.models import ClipboardEntry
        entry = ClipboardEntry(
            content_type=TYPE_VIDEO, content=r"C:\media\演示.mp4",
            preview="[视频] 演示.mp4", timestamp=1)
        entry.validate()
        saved_id = kernel.repo.save(entry)
        fetched = kernel.repo.find_by_id(saved_id)
        check(fetched is not None and fetched.content_type == TYPE_VIDEO,
              "视频记录能正常入库并读回")
        check(fetched.to_dict()["typeLabel"] == "视频",
              "记录字典里带「视频」标签：%s" % fetched.to_dict()["typeLabel"])

        # 注意：`total` 是**整个库的条数**（界面状态栏显示"图片 3/100 条"
        # 就是这个意思），不随类型筛选变化；分页靠 len(items) 判断。
        # 所以这里要断言的是 items，不是 total。
        videos = kernel.get_history(content_type=TYPE_VIDEO)["data"]
        check(len(videos["items"]) == 1,
              "能按 video 类型筛出这条记录（%d 条）" % len(videos["items"]))
        check(videos["items"][0]["contentType"] == TYPE_VIDEO,
              "筛出来的确实是视频")
        files = kernel.get_history(content_type=TYPE_FILE)["data"]
        check(len(files["items"]) == 0,
              "**按 file 筛选不会把视频混进来**（items 为空，total=%d 是全库条数）"
              % files["total"])

        # 删除时附件清理不该把不存在的路径当回事
        deleted = kernel.delete_entry(saved_id)
        check(deleted["ok"], "视频记录能正常删除")

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
