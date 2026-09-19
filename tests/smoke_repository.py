# -*- coding: utf-8 -*-
"""仓储层冒烟自检：不依赖界面与剪贴板，直接验证建库、写入、去重、裁剪、检索。

用法：
    python -m tests.smoke_repository
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.constants import TYPE_IMAGE, TYPE_TEXT  # noqa: E402
from app.domain.models import ClipboardEntry, make_preview  # noqa: E402
from app.errors import now_ms  # noqa: E402
from app.infrastructure.database import Database  # noqa: E402
from app.infrastructure.repository import migrations  # noqa: E402
from app.infrastructure.repository.clipboard_repo import ClipboardRepository  # noqa: E402
from app.infrastructure.repository.settings_repo import SettingsRepository  # noqa: E402


def check(condition, message):
    if condition:
        print("  [OK]   %s" % message)
    else:
        print("  [FAIL] %s" % message)
        raise AssertionError(message)


def main():
    tmp = tempfile.mkdtemp(prefix="clipboardpro_smoke_")
    db = Database(data_dir=tmp)
    executed = migrations.run_migrations(db.conn)
    print("结构升级执行版本：%s（当前版本 %s）" % (executed, migrations.CURRENT_VERSION))
    check(executed == [1], "首次启动完成 v1 建库建表")
    check(migrations.run_migrations(db.conn) == [], "重复启动不重复执行升级脚本")

    repo = ClipboardRepository(db)
    settings = SettingsRepository(db)
    settings.load_all()
    check(settings.get_int("app.persistent_limit") == 500, "配置默认值已写入（上限 500）")
    check(settings.get("app.theme") == "mica", "默认主题为云母")
    check(len(settings.snapshot()) == len(settings.describe()), "配置清单完整")

    def new_entry(text, **kwargs):
        entry = ClipboardEntry(
            content_type=kwargs.get("content_type", TYPE_TEXT),
            content=text,
            preview=make_preview(text),
            timestamp=kwargs.get("timestamp", now_ms()),
            source_app=kwargs.get("source_app", "记事本"),
            content_hash=kwargs.get("content_hash", hash(text) & 0x7FFFFFFFFFFFFFFF),
        )
        return entry

    first = new_entry("第一条内容")
    first_id = repo.save(first)
    check(first_id > 0, "插入记录返回自增 id = %s" % first_id)
    check(repo.count() == 1, "记录总数为 1")

    dup = repo.find_duplicate(TYPE_TEXT, first.content_hash)
    check(dup is not None and dup.id == first_id, "按类型+指纹能查到重复记录")

    repo.touch_entry(first_id, source_app="Chrome")
    merged = repo.get_entry(first_id)
    check(merged.use_count == 1, "合并刷新后使用次数累计为 1")
    check(merged.source_app == "Chrome", "合并刷新后来源应用更新")
    check(repo.count() == 1, "合并刷新不新增记录行")

    for i in range(5):
        repo.save(new_entry("批量内容 %d" % i, timestamp=now_ms() + i))

    latest = repo.get_history(limit=3)
    check(len(latest) == 3, "分页读取返回 3 条")
    check(latest[0].content == "批量内容 4", "列表按时间倒序")

    pinned = repo.toggle_pin(latest[-1].id, True)
    check(pinned.is_pinned == 1 and pinned.pinned_order > 0, "置顶成功且写入置顶顺序")
    ordered = repo.get_history(limit=10)
    check(ordered[0].id == latest[-1].id, "置顶记录排在列表最前")

    tagged = repo.add_tag(latest[0].id, "重要")
    check(tagged.tags == ["重要"], "添加标签成功")
    check(repo.list_tags()[0]["name"] == "重要", "标签定义表已写入且带使用次数")
    try:
        repo.add_tag(latest[0].id, "")
        check(False, "空标签名应被拒绝")
    except Exception:
        check(True, "空标签名被拒绝")

    found = repo.search("批量内容 2")
    check(len(found) == 1 and found[0].content == "批量内容 2", "正文检索命中 1 条")
    found = repo.search("重要", tag_only=True)
    check(len(found) == 1, "仅标签检索命中 1 条")
    found = repo.search("Chrome")
    check(len(found) == 1, "来源应用检索命中 1 条")
    found = repo.search("%")
    check(len(found) == 0, "SQL 通配符被转义，% 不再匹配全部记录")

    counts = repo.count_by_type()
    check(counts.get(TYPE_TEXT) == 6, "类型计数正确：%s" % counts)

    removed = repo.clear(keep_protected=True)
    check(len(removed) == 4, "清空历史保留置顶与已打标签记录，删除 4 条")
    check(repo.count() == 2, "清空后剩余 2 条（1 置顶 + 1 带标签）")

    usage = repo.storage_usage(db.attachment_dir)
    check(usage["entryCount"] == 2, "容量统计条数正确：%s" % usage)

    repo.delete(ordered[0].id, db.attachment_dir)
    check(repo.count() == 1, "单条删除成功")

    image = ClipboardEntry(
        content_type=TYPE_IMAGE,
        content=os.path.join(db.attachment_dir, "deadbeef.png"),
        preview="[图片]",
        timestamp=now_ms(),
        content_hash=12345,
    )
    repo.save(image)
    check(repo.count() == 2, "图片类记录可入库（content 存附件路径）")

    try:
        repo.get_entry(999999)
        check(False, "读取不存在的记录应抛错")
    except Exception as exc:
        check("不存在" in str(exc), "读取不存在的记录抛出业务异常：%s" % exc)

    db.close()
    print("\n全部通过。数据目录：%s" % tmp)


if __name__ == "__main__":
    main()
