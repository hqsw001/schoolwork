# -*- coding: utf-8 -*-
"""主题令牌系统（F8-1 多套外观主题 / F8-2 深浅色模式）

结构照搬开源参照项目 TieZ 的两层设计（《需求分析报告》3.6 令牌化主题系统）：
    公共层定义**语义令牌**（bg / surface / text / accent …），
    主题层只覆盖这些令牌的取值。
新增主题不需要改任何组件代码，只要在这里加一份令牌表。

本期状态（用户已确认第一周只做核心链路）：
    ✅ 六套主题的令牌表全部就位，切换即时生效
    ⏳ 云母 / 毛玻璃需要的系统窗口材质（DWM）留到第三周，
       在不支持的 Windows 版本上本来就应该自动降级为普通半透明背景（UC-15 备选流 A4）
"""

from app.constants import MODE_DARK, MODE_LIGHT, MODE_SYSTEM

#: 令牌说明——所有主题必须提供同名令牌，缺一个就会在界面上出现"裸色块"
TOKEN_KEYS = (
    "bg",            # 面板底色
    "surface",       # 卡片底色
    "surface_alt",   # 次级区域（页脚、过滤条）
    "surface_hover", # 悬停
    "surface_sel",   # 选中
    "border",        # 边框与分隔线
    "text",          # 主文字
    "text_muted",    # 次要文字
    "text_faint",    # 更弱的提示文字
    "accent",        # 主题色（选中边、按钮）
    "accent_text",   # 主题色上的文字
    "pin",           # 置顶标记
    "sensitive",     # 敏感标记
    "badge_text",    # 类型徽标底色（文本类）
    "badge_rich",    # 类型徽标底色（富文本）
    "badge_code",    # 类型徽标底色（代码）
    "badge_url",     # 类型徽标底色（网址）
    "badge_image",   # 类型徽标底色（图片）
    "badge_file",    # 类型徽标底色（文件）
    "hit",           # 检索命中高亮底色
    "hit_text",      # 检索命中高亮文字
    "font_family",   # 字体族
    "radius",        # 圆角（tkinter 不支持真正圆角，仅用于留白风格）
)

THEMES = {
    # ---------------------------------------------------------------- 云母（默认）
    "mica": {
        "label": "云母",
        "light": {
            "bg": "#F3F3F3", "surface": "#FBFBFB", "surface_alt": "#EFEFEF",
            "surface_hover": "#E9E9E9", "surface_sel": "#E3EEFB",
            "border": "#DCDCDC", "text": "#1B1B1B", "text_muted": "#5A5A5A",
            "text_faint": "#8A8A8A", "accent": "#0F6CBD", "accent_text": "#FFFFFF",
            "pin": "#C77700", "sensitive": "#C42B1C",
            "badge_text": "#4A5A6A", "badge_rich": "#7A5AA8", "badge_code": "#0F6CBD",
            "badge_url": "#0E7A5F", "badge_image": "#B06000", "badge_file": "#6A6A6A",
            "hit": "#FFE9A8", "hit_text": "#3A2B00",
            "font_family": "Microsoft YaHei UI", "radius": 8,
        },
        "dark": {
            "bg": "#202020", "surface": "#2B2B2B", "surface_alt": "#262626",
            "surface_hover": "#333333", "surface_sel": "#1F3A55",
            "border": "#3A3A3A", "text": "#F2F2F2", "text_muted": "#B8B8B8",
            "text_faint": "#8C8C8C", "accent": "#4CC2FF", "accent_text": "#00243D",
            "pin": "#F0B429", "sensitive": "#FF6B60",
            "badge_text": "#A9BACB", "badge_rich": "#C3A6E8", "badge_code": "#4CC2FF",
            "badge_url": "#57D2A8", "badge_image": "#F0B429", "badge_file": "#B0B0B0",
            "hit": "#5A4A00", "hit_text": "#FFE9A8",
            "font_family": "Microsoft YaHei UI", "radius": 8,
        },
    },
    # ---------------------------------------------------------------- 3D 复古
    "3d": {
        "label": "3D 复古",
        "light": {
            "bg": "#C6C6C6", "surface": "#DCDCDC", "surface_alt": "#BEBEBE",
            "surface_hover": "#D2D2D2", "surface_sel": "#9FB6C9",
            "border": "#8C8C8C", "text": "#141414", "text_muted": "#3C3C3C",
            "text_faint": "#5E5E5E", "accent": "#1F4E79", "accent_text": "#FFFFFF",
            "pin": "#8A5A00", "sensitive": "#8A1A10",
            "badge_text": "#3A4652", "badge_rich": "#5A3E7A", "badge_code": "#1F4E79",
            "badge_url": "#155E4A", "badge_image": "#8A5A00", "badge_file": "#4A4A4A",
            "hit": "#FFD75E", "hit_text": "#1A1200",
            "font_family": "Tahoma", "radius": 2,
        },
        "dark": {
            "bg": "#2E2E2E", "surface": "#3C3C3C", "surface_alt": "#333333",
            "surface_hover": "#474747", "surface_sel": "#274B69",
            "border": "#565656", "text": "#EDEDED", "text_muted": "#C0C0C0",
            "text_faint": "#909090", "accent": "#5AA9E6", "accent_text": "#0B2133",
            "pin": "#E0A63A", "sensitive": "#E46A5C",
            "badge_text": "#B9C4CE", "badge_rich": "#C9AEE8", "badge_code": "#5AA9E6",
            "badge_url": "#5ACCA6", "badge_image": "#E0A63A", "badge_file": "#ADADAD",
            "hit": "#6A5200", "hit_text": "#FFE9A8",
            "font_family": "Tahoma", "radius": 2,
        },
    },
    # ---------------------------------------------------------------- 毛玻璃
    "glass": {
        "label": "毛玻璃",
        "light": {
            "bg": "#EAF1F6", "surface": "#F7FBFE", "surface_alt": "#E4EDF4",
            "surface_hover": "#EAF3FA", "surface_sel": "#D6EAF8",
            "border": "#CFE0EC", "text": "#14212B", "text_muted": "#4B6070",
            "text_faint": "#7C8FA0", "accent": "#2C7BE5", "accent_text": "#FFFFFF",
            "pin": "#C08A00", "sensitive": "#C42B1C",
            "badge_text": "#4E6273", "badge_rich": "#7159A8", "badge_code": "#2C7BE5",
            "badge_url": "#11806A", "badge_image": "#B07A00", "badge_file": "#67788A",
            "hit": "#FFEFB0", "hit_text": "#3A2B00",
            "font_family": "Microsoft YaHei UI", "radius": 12,
        },
        "dark": {
            "bg": "#182028", "surface": "#212C36", "surface_alt": "#1C252D",
            "surface_hover": "#2A3742", "surface_sel": "#1D3B54",
            "border": "#2F3E4A", "text": "#EAF2F8", "text_muted": "#A9BCCA",
            "text_faint": "#7E93A3", "accent": "#5AB0F0", "accent_text": "#062033",
            "pin": "#E8B84B", "sensitive": "#FF7B70",
            "badge_text": "#AFC3D2", "badge_rich": "#C4ABE8", "badge_code": "#5AB0F0",
            "badge_url": "#5FD3AE", "badge_image": "#E8B84B", "badge_file": "#A6B6C4",
            "hit": "#54470A", "hit_text": "#FFEFB0",
            "font_family": "Microsoft YaHei UI", "radius": 12,
        },
    },
    # ---------------------------------------------------------------- 便利贴
    "sticky": {
        "label": "便利贴",
        "light": {
            "bg": "#FFF6C9", "surface": "#FFFBE0", "surface_alt": "#FBEFAE",
            "surface_hover": "#FFF3B8", "surface_sel": "#F6E28A",
            "border": "#E6D48A", "text": "#3A3000", "text_muted": "#6B5C14",
            "text_faint": "#948545", "accent": "#B07A00", "accent_text": "#FFFFFF",
            "pin": "#8A5A00", "sensitive": "#A8281C",
            "badge_text": "#6B5C14", "badge_rich": "#7A5AA8", "badge_code": "#1F5E8C",
            "badge_url": "#12684F", "badge_image": "#8A5A00", "badge_file": "#6A5C2A",
            "hit": "#FFC94D", "hit_text": "#3A2B00",
            "font_family": "KaiTi", "radius": 6,
        },
        "dark": {
            "bg": "#3A3416", "surface": "#463F1C", "surface_alt": "#332E13",
            "surface_hover": "#524A22", "surface_sel": "#5C5227",
            "border": "#5E5426", "text": "#FBF3CE", "text_muted": "#D6C98C",
            "text_faint": "#A79A5F", "accent": "#E8B84B", "accent_text": "#2A2200",
            "pin": "#E8B84B", "sensitive": "#FF7B70",
            "badge_text": "#D6C98C", "badge_rich": "#C4ABE8", "badge_code": "#7FC4F0",
            "badge_url": "#7FD3AE", "badge_image": "#E8B84B", "badge_file": "#C0B48A",
            "hit": "#6A5A10", "hit_text": "#FFF3C4",
            "font_family": "KaiTi", "radius": 6,
        },
    },
    # ---------------------------------------------------------------- 纸质书感
    "book": {
        "label": "纸质书感",
        "light": {
            "bg": "#F4EFE4", "surface": "#FBF7EE", "surface_alt": "#EDE6D6",
            "surface_hover": "#F5EFE2", "surface_sel": "#E8DFC8",
            "border": "#DCD2BC", "text": "#2E2A22", "text_muted": "#5E5648",
            "text_faint": "#8B8272", "accent": "#8A5A2B", "accent_text": "#FFFFFF",
            "pin": "#9A6A00", "sensitive": "#A33122",
            "badge_text": "#5E5648", "badge_rich": "#6E4C8A", "badge_code": "#3D5A80",
            "badge_url": "#2F6B52", "badge_image": "#8A5A2B", "badge_file": "#6B6353",
            "hit": "#F0DCA0", "hit_text": "#3A2B00",
            "font_family": "SimSun", "radius": 3,
        },
        "dark": {
            "bg": "#26231D", "surface": "#302C24", "surface_alt": "#211E19",
            "surface_hover": "#3A352B", "surface_sel": "#4A4133",
            "border": "#443E33", "text": "#EDE7DA", "text_muted": "#BFB6A3",
            "text_faint": "#8E8676", "accent": "#D9A15E", "accent_text": "#2A1E0C",
            "pin": "#D9A15E", "sensitive": "#E4766A",
            "badge_text": "#BFB6A3", "badge_rich": "#C0A6DE", "badge_code": "#8FB4D9",
            "badge_url": "#86C7A6", "badge_image": "#D9A15E", "badge_file": "#AEA694",
            "hit": "#5C4A18", "hit_text": "#F4E3B4",
            "font_family": "SimSun", "radius": 3,
        },
    },
    # ---------------------------------------------------------------- 樱花
    "sakura": {
        "label": "樱花",
        "light": {
            "bg": "#FDF1F4", "surface": "#FFF8FA", "surface_alt": "#FBE7ED",
            "surface_hover": "#FCEDF1", "surface_sel": "#F7D6E0",
            "border": "#F0D3DC", "text": "#3A2530", "text_muted": "#6E4C5A",
            "text_faint": "#9C7A88", "accent": "#D6486E", "accent_text": "#FFFFFF",
            "pin": "#C08A00", "sensitive": "#C42B1C",
            "badge_text": "#6E4C5A", "badge_rich": "#8A5AA8", "badge_code": "#3D6EA8",
            "badge_url": "#2E7D66", "badge_image": "#C08A00", "badge_file": "#7A6672",
            "hit": "#FFD9E4", "hit_text": "#5A0F24",
            "font_family": "Microsoft YaHei UI", "radius": 12,
        },
        "dark": {
            "bg": "#2A1E23", "surface": "#35262C", "surface_alt": "#251A1F",
            "surface_hover": "#3F2D34", "surface_sel": "#54303C",
            "border": "#4A343C", "text": "#FAEAF0", "text_muted": "#D3B4C0",
            "text_faint": "#A4848F", "accent": "#F07EA0", "accent_text": "#3A0C1C",
            "pin": "#E8B84B", "sensitive": "#FF7B70",
            "badge_text": "#D3B4C0", "badge_rich": "#C9AEE8", "badge_code": "#8FBEE8",
            "badge_url": "#86CFB4", "badge_image": "#E8B84B", "badge_file": "#BFAAB3",
            "hit": "#5E2436", "hit_text": "#FFD9E4",
            "font_family": "Microsoft YaHei UI", "radius": 12,
        },
    },
}

#: 默认主题（配置项被外部改坏时的回退目标，UC-15 备选流 A1）
DEFAULT_THEME = "mica"

#: 列表密度的内边距与行高（像素）
DENSITY_METRICS = {
    "compact": {"pad_y": 4, "preview_lines": 1, "gap": 3, "font_delta": -1},
    "normal": {"pad_y": 7, "preview_lines": 2, "gap": 5, "font_delta": 0},
    "loose": {"pad_y": 11, "preview_lines": 3, "gap": 8, "font_delta": 1},
}


def resolve_mode(mode):
    """把配置里的深浅色模式解析成 light / dark。

    system 模式下读注册表判断系统偏好；读不到就按浅色处理。
    """
    if mode == MODE_DARK:
        return "dark"
    if mode == MODE_LIGHT:
        return "light"
    if mode != MODE_SYSTEM:
        return "light"
    return _system_mode()


def _system_mode():
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        try:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if int(value) != 0 else "dark"
        finally:
            winreg.CloseKey(key)
    except Exception:  # noqa: BLE001 - 读不到就按浅色，不影响可用性
        return "light"


def get_tokens(theme_id, mode):
    """取指定主题与模式下的完整令牌表；主题标识非法时回退默认主题。"""
    theme = THEMES.get(theme_id)
    if theme is None:
        theme = THEMES[DEFAULT_THEME]
    resolved = resolve_mode(mode)
    tokens = dict(theme.get(resolved) or theme["light"])
    tokens["mode"] = resolved
    tokens["theme"] = theme_id if theme_id in THEMES else DEFAULT_THEME
    tokens["label"] = theme.get("label", DEFAULT_THEME)
    return tokens


def list_themes():
    """主题清单（供设置面板渲染缩略图卡片）。"""
    return [{"id": key, "label": value["label"]} for key, value in THEMES.items()]
