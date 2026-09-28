"""Generate PNKX life-domain Skill packages (SKILL.md + aria-api.yaml).

Source of truth: controller survey of /Users/peihaoyu/PHY/pnkx/code/pnkx-admin (2026-09-28).
All contracts use the admin-configured generic HTTPS connection `pnkx-admin`
(login_bearer via /clientLogin). GET side-effect endpoints are excluded.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import yaml

from app.skills.importer import import_skill_zip
from app.skills.models import SkillDocument

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "examples"

AUTH = {
    "type": "login_bearer",
    "path": "/clientLogin",
    "username_field": "userName",
    "password_field": "password",
    "token_field": "token",
}
DEFAULT_CONNECTION = "pnkx-admin"


def q(ptype: str = "string", required: bool = False) -> tuple[str, str, bool]:
    return (ptype, "query", required)


def p(ptype: str = "integer") -> tuple[str, str, bool]:
    return (ptype, "path", True)


def op(
    name: str,
    description: str,
    method: str,
    path: str,
    risk: str,
    params: dict[str, tuple[str, str, bool]] | None = None,
) -> dict:
    parameters = {}
    for pname, (ptype, loc, required) in (params or {}).items():
        parameters[pname] = {"type": ptype, "required": required, "location": loc}
    return {
        "name": name,
        "description": description,
        "method": method,
        "path": path,
        "risk": risk,
        "parameters": parameters,
    }


PAGE = {"pageNum": q("integer"), "pageSize": q("integer")}


def merge(base: dict, extra: dict) -> dict:
    return {**base, **extra}


Skill = tuple[str, str, str, str, list[dict]]  # name, connection, description, instructions, operations
SKILLS: list[Skill] = [
    (
        "pnkx-todo",
        "pnkx-life",
        "查询和管理 PNKX 情侣待办：列表、看板、标签、子任务。",
        """用户询问待办、任务、今天要做的事时，优先调用待办列表或看板查询；询问标签时调用标签列表；询问某任务的子任务时调用子任务查询。接口按登录令牌识别当前用户，不要传入他人 ID。写操作（新增/修改/删除/排序）已登记但只在确认链接通后使用，当前对话中不得声称已修改。响应为空或失败时如实说明。""",
        [
            op("todo_list", "分页查询当前用户的待办列表", "GET", "/admin/toDo/list", "read",
               merge({"content": q(), "performer": q(), "status": q(),
                      "label": q(), "priority": q(), "kanbanStatus": q()}, PAGE)),
            op("todo_detail", "按 ID 查询待办详情", "GET", "/admin/toDo/{id}", "read", {"id": p()}),
            op("todo_label_list", "获取当前用户待办标签（去重）", "GET", "/admin/toDo/getLabelList", "read"),
            op("todo_kanban", "看板三栏（待办/进行中/已完成）查询", "GET", "/admin/toDo/kanban", "read",
               {"label": q(), "priority": q(), "kanbanStatus": q()}),
            op("todo_subtasks", "查询指定父任务的子任务", "GET", "/admin/toDo/subtask/{parentId}", "read",
               {"parentId": p()}),
            op("todo_add", "新增待办（待接入确认链）", "POST", "/admin/toDo", "confirm"),
            op("todo_edit", "修改待办（待接入确认链）", "PUT", "/admin/toDo", "confirm"),
            op("todo_remove", "批量删除待办（待接入确认链）", "DELETE", "/admin/toDo/{ids}", "confirm",
               {"ids": p("string")}),
            op("todo_sort", "批量更新看板状态与排序（待接入确认链）", "PUT", "/admin/toDo/sort", "confirm"),
        ],
    ),
    (
        "pnkx-subscription",
        "pnkx-life",
        "查询 PNKX 订阅服务与月度/年度费用预测。",
        """用户询问订阅了什么服务、下次扣费、每月/每年订阅总花费时，调用订阅列表或费用预测。forecast 返回启用订阅的月均总额、年度总额与逐条明细，直接据此汇总，不要自行心算。写操作未接入执行链。""",
        [
            op("subscription_list", "分页查询订阅列表", "GET", "/subscription/list", "read",
               merge({"name": q(), "cycle": q(), "enabled": q()}, PAGE)),
            op("subscription_detail", "订阅详情", "GET", "/subscription/{id}", "read", {"id": p()}),
            op("subscription_forecast", "月度/年度订阅费用预测汇总", "GET", "/subscription/forecast", "read"),
            op("subscription_add", "新增订阅（待接入确认链）", "POST", "/subscription", "confirm"),
            op("subscription_edit", "修改订阅（待接入确认链）", "PUT", "/subscription", "confirm"),
            op("subscription_remove", "批量删除订阅（待接入确认链）", "DELETE", "/subscription/{ids}",
               "confirm", {"ids": p("string")}),
        ],
    ),
    (
        "pnkx-calendar",
        "pnkx-life",
        "查询 PNKX 情侣日历：今日驾驶舱和按月聚合事件。",
        """用户问“今天有什么安排”时调用今日驾驶舱；问某月日历、某天前后事件时调用按月聚合（startDate/endDate 用 yyyy-MM-dd，缺省为当月）。聚合结果包含待办、纪念日、经期、记账事件，按用户问题择要说明。""",
        [
            op("calendar_cockpit", "今日概览：紧迫任务与下一个纪念日", "GET", "/calendar/cockpit", "read"),
            op("calendar_month", "按月范围查询聚合日历事件", "GET", "/calendar/month", "read",
               {"startDate": q(), "endDate": q()}),
        ],
    ),
    (
        "pnkx-reminder",
        "pnkx-life",
        "查询 PNKX 提醒中心：今日提醒、通知、未读数与提醒偏好。",
        """用户问“今天有什么提醒”时调用今日提醒（聚合纪念日/卡券/经期/待办）；问消息通知时调用通知列表或未读数。偏好与微信订阅状态只读说明，修改偏好的写操作未接入执行链。""",
        [
            op("reminder_today", "今日提醒聚合（首页数据源）", "GET", "/reminder/today", "read"),
            op("reminder_list", "分页查询当前用户提醒配置", "GET", "/reminder/list", "read",
               merge({"sourceType": q(), "enabled": q()}, PAGE)),
            op("reminder_notifications", "当前用户通知列表", "GET", "/reminder/notifications", "read"),
            op("reminder_unread_count", "当前用户未读通知数", "GET", "/reminder/unread/count", "read"),
            op("reminder_preference", "当前用户提醒偏好", "GET", "/reminder/preference", "read"),
            op("reminder_wechat_subscription", "微信订阅消息选择状态", "GET", "/reminder/wechat-subscription", "read"),
            op("reminder_bind", "为来源实体绑定提醒（待接入确认链）", "POST", "/reminder/bind", "confirm"),
            op("reminder_save_preference", "保存提醒偏好（待接入确认链）", "PUT", "/reminder/preference", "confirm"),
        ],
    ),
    (
        "pnkx-commemoration",
        "pnkx-life",
        "查询 PNKX 情侣纪念日列表与详情。",
        """用户问纪念日、倒数日、即将到来的特殊日期时调用纪念日列表（可按名称筛选），据实时数据说明日期与重复规则。写操作未接入执行链。""",
        [
            op("commemoration_list", "分页查询纪念日", "GET", "/commemorationDay/list", "read",
               merge({"name": q(), "repeat": q()}, PAGE)),
            op("commemoration_detail", "纪念日详情", "GET", "/commemorationDay/{id}", "read", {"id": p()}),
            op("commemoration_add", "新增纪念日（待接入确认链）", "POST", "/commemorationDay", "confirm"),
            op("commemoration_edit", "修改纪念日（待接入确认链）", "PUT", "/commemorationDay", "confirm"),
            op("commemoration_remove", "批量删除纪念日（待接入确认链）", "DELETE", "/commemorationDay/{ids}",
               "confirm", {"ids": p("string")}),
        ],
    ),
    (
        "pnkx-menstruation",
        "pnkx-life",
        "查询 PNKX 生理期记录与最近一次经期开始。",
        """仅在用户明确询问生理期/经期相关记录时调用列表（可按 date、type 筛选）或“最后开始日”。属于高隐私数据，只在 L1 且用户主动询问时使用，不复述多余细节。""",
        [
            op("menstruation_list", "分页查询生理期记录", "GET", "/myTool/menstruationRecord/list", "read",
               merge({"date": q(), "type": q(), "state": q()}, PAGE)),
            op("menstruation_list_extended", "分页查询生理期记录（扩展查询）", "GET",
               "/myTool/menstruationRecord/getPxMenstruationRecordList", "read",
               merge({"date": q(), "type": q()}, PAGE)),
            op("menstruation_last_start", "最后一次经期开始的记录", "GET",
               "/myTool/menstruationRecord/getLastStartDate", "read"),
            op("menstruation_detail", "记录详情", "GET", "/myTool/menstruationRecord/{id}", "read", {"id": p()}),
        ],
    ),
    (
        "pnkx-note",
        "pnkx-life",
        "查询 PNKX 笔记与笔记文件夹树。",
        """用户问笔记、备忘录内容时调用笔记列表或详情；问目录结构时调用文件夹树。写操作与回收站类操作未接入执行链。""",
        [
            op("note_list", "分页查询笔记", "GET", "/note/list", "read",
               merge({"title": q(), "folder": q()}, PAGE)),
            op("note_detail", "笔记详情", "GET", "/note/{id}", "read", {"id": p()}),
            op("note_folder_list", "分页查询笔记文件夹", "GET", "/note/folder/list", "read",
               merge({"name": q()}, PAGE)),
            op("note_folder_tree", "笔记文件夹树（不分页）", "GET", "/note/folder/treeList", "read"),
            op("note_add", "新增笔记（待接入确认链）", "POST", "/note", "confirm"),
            op("note_edit", "修改笔记（待接入确认链）", "PUT", "/note", "confirm"),
        ],
    ),
    (
        "pnkx-diary",
        "pnkx-life",
        "查询 PNKX 情侣日记：列表、检索、关键词搜索与心情分析。",
        """用户问写了什么日记、某天心情时调用日记列表（可按 date、mood 筛选）或按关键字检索；问心情分布时调用分析数据（该接口返回心情统计+时间线分页）。SSE 流式分析接口不在只读契约内。""",
        [
            op("diary_list", "分页查询日记", "GET", "/admin/diary/list", "read",
               merge({"title": q(), "mood": q(), "weather": q(), "date": q()}, PAGE)),
            op("diary_detail", "日记详情", "GET", "/admin/diary/{id}", "read", {"id": p()}),
            op("diary_retrieval", "按关键字检索日记（不分页）", "GET", "/admin/diary/retrieval", "read",
               {"searchCode": q()}),
            op("diary_analysis_data", "日记心情分析数据与时间线", "GET", "/diary/analysis/data", "read",
               {"isAll": q(), "pageNum": q(), "pageSize": q(),
                "timelineOnly": q()}),
            op("diary_add", "新增日记（待接入确认链）", "POST", "/admin/diary", "confirm"),
            op("diary_edit", "修改日记（待接入确认链）", "PUT", "/admin/diary", "confirm"),
        ],
    ),
    (
        "pnkx-bookkeeping",
        "pnkx-life",
        "查询 PNKX 记账：流水、分类/账户树、预算、周期账、模板与资产统计。",
        """用户问花了多少、本月账单、预算使用情况时分别调用账单列表（payTime 用 yyyy-MM 按月筛选）、分类树、账户列表、预算状态、资产统计。资产统计是只读语义的 POST，仅在工具可用时调用。写操作未接入执行链，不得声称已记账。""",
        [
            op("bill_list", "分页查询账单流水", "GET", "/bookkeeping/record/list", "read",
               merge({"payTime": q(), "type": q(), "account": q(),
                      "money": q()}, PAGE)),
            op("bill_detail", "账单详情", "GET", "/bookkeeping/record/{id}", "read", {"id": p()}),
            op("classification_tree", "一二级分类树与最近使用", "GET",
               "/bookkeeping/classification/getClassificationList", "read"),
            op("account_list", "账户列表（含最近使用分组）", "GET", "/bookkeeping/account/getAccountList", "read"),
            op("budget_status", "某月预算使用状态", "GET", "/bookkeeping/budget/status", "read",
               {"month": q("string", True)}),
            op("budget_list", "某月预算配置列表", "GET", "/bookkeeping/budget/list", "read",
               {"month": q("string", True)}),
            op("recurring_list", "周期记账规则列表", "GET", "/bookkeeping/recurring/list", "read"),
            op("record_model_list", "账单模板列表", "GET", "/bookkeeping/recordModel/list", "read",
               dict(PAGE)),
            op("assets_statistics", "资产总览统计", "POST", "/bookkeeping/statistics/getAssetsStatistics",
               "confirm"),
            op("bill_add", "新增账单（待接入确认链）", "POST", "/bookkeeping/record", "confirm"),
            op("bill_edit", "修改账单（待接入确认链）", "PUT", "/bookkeeping/record", "confirm"),
        ],
    ),
    (
        "pnkx-recipe-meal",
        "pnkx-food",
        "查询 PNKX 菜谱与餐饮计划（含周视图）。",
        """用户问会做什么菜、某菜谱详情时调用菜谱列表/含食材详情；问这周吃什么时调用餐饮计划周视图（startDate/endDate 为 yyyy-MM-dd，必填）。写操作与转入购物清单动作未接入执行链。""",
        [
            op("recipe_list", "分页查询菜谱", "GET", "/recipe/list", "read",
               merge({"title": q()}, PAGE)),
            op("recipe_detail", "菜谱详情", "GET", "/recipe/{id}", "read", {"id": p()}),
            op("recipe_with_ingredients", "菜谱详情（含食材列表）", "GET", "/recipe/withIngredients/{id}",
               "read", {"id": p()}),
            op("meal_plan_list", "分页查询餐饮计划", "GET", "/mealPlan/list", "read",
               merge({"planDate": q(), "mealType": q()}, PAGE)),
            op("meal_plan_detail", "餐饮计划详情", "GET", "/mealPlan/{id}", "read", {"id": p()}),
            op("meal_plan_week", "按日期范围查询周餐饮计划", "GET", "/mealPlan/week", "read",
               {"startDate": q("string", True), "endDate": q("string", True)}),
            op("recipe_add", "新增菜谱（待接入确认链）", "POST", "/recipe", "confirm"),
            op("meal_plan_add", "新增餐饮计划（待接入确认链）", "POST", "/mealPlan", "confirm"),
            op("meal_plan_edit", "修改餐饮计划（待接入确认链）", "PUT", "/mealPlan", "confirm"),
            op("meal_plan_remove", "批量删除餐饮计划（待接入确认链）", "DELETE", "/mealPlan/{ids}",
               "confirm", {"ids": p("string")}),
        ],
    ),
    (
        "pnkx-shopping",
        "pnkx-food",
        "查询 PNKX 购物清单与清单条目。",
        """用户问要买什么、清单里还剩什么时调用清单列表和条目列表（按 listId 过滤，checked=false 表示未买）。写操作与清空已勾选未接入执行链。""",
        [
            op("shopping_list", "分页查询购物清单", "GET", "/shoppingList/list", "read",
               merge({"name": q()}, PAGE)),
            op("shopping_list_detail", "购物清单详情", "GET", "/shoppingList/{id}", "read", {"id": p()}),
            op("shopping_item_list", "分页查询清单条目", "GET", "/shoppingItem/list", "read",
               merge({"listId": q(), "name": q(), "checked": q()}, PAGE)),
            op("shopping_item_detail", "条目详情", "GET", "/shoppingItem/{id}", "read", {"id": p()}),
            op("shopping_item_add", "新增条目（待接入确认链）", "POST", "/shoppingItem", "confirm"),
            op("shopping_item_edit", "修改条目（待接入确认链）", "PUT", "/shoppingItem", "confirm"),
            op("shopping_list_add", "新增清单（待接入确认链）", "POST", "/shoppingList", "confirm"),
        ],
    ),
    (
        "pnkx-book",
        "pnkx-food",
        "查询 PNKX 个人书架：书籍、章节与阅读进度。",
        """用户问在读什么书、书单、某书章节时调用书籍列表/章节列表；阅读进度数据可通过章节阅读器接口获取。列表与详情均按当前登录用户过滤。TXT 导入导出与写操作未接入执行链。""",
        [
            op("book_list", "分页查询我的书籍", "GET", "/myBook/list", "read",
               merge({"title": q(), "author": q(), "status": q()}, PAGE)),
            op("book_detail", "书籍详情", "GET", "/myBook/{id}", "read", {"id": p()}),
            op("book_chapter_list", "分页查询书籍章节", "GET", "/myBook/chapter/list", "read",
               merge({"bookId": q(), "chapterName": q()}, PAGE)),
            op("book_chapter_detail", "章节详情", "GET", "/myBook/chapter/{id}", "read", {"id": p()}),
            op("book_chapter_reader", "章节阅读数据（正文+进度）", "GET", "/myBook/chapter/{id}/reader",
               "read", {"id": p()}),
            op("book_add", "新增书籍（待接入确认链）", "POST", "/myBook", "confirm"),
            op("book_edit", "修改书籍（待接入确认链）", "PUT", "/myBook", "confirm"),
            op("book_progress_update", "更新阅读进度（待接入确认链）", "PUT", "/myBook/progress/{chapterId}",
               "confirm", {"chapterId": p()}),
        ],
    ),
    (
        "pnkx-wallpaper",
        "pnkx-media",
        "查询 PNKX 壁纸库与当前用户的点赞、下载记录。",
        """用户问有什么壁纸、想收藏/下载过什么时调用壁纸列表、我的点赞、我的下载、收藏状态查询。二进制下载接口不在只读契约内；点赞是写操作未接入执行链。""",
        [
            op("wallpaper_list", "分页查询壁纸", "GET", "/client/wallpaper/list", "read",
               merge({"name": q(), "folder": q()}, PAGE)),
            op("wallpaper_detail", "壁纸详情", "GET", "/client/wallpaper/{id}", "read", {"id": p()}),
            op("wallpaper_folder_list", "分页查询壁纸文件夹", "GET", "/client/wallpaper/folder/list", "read",
               dict(PAGE)),
            op("wallpaper_collect_check", "当前用户对某壁纸的点赞/收藏状态", "GET",
               "/client/wallpaper/collectCheck/{id}", "read", {"id": p()}),
            op("wallpaper_my_likes", "当前用户点赞记录", "GET", "/client/wallpaper/myLikes", "read",
               dict(PAGE)),
            op("wallpaper_my_downloads", "当前用户下载记录", "GET", "/client/wallpaper/myDownloads", "read",
               dict(PAGE)),
            op("wallpaper_like_toggle", "点赞切换（待接入确认链）", "POST", "/client/wallpaper/like/{id}",
               "confirm", {"id": p()}),
        ],
    ),
    (
        "pnkx-blog-content",
        "pnkx-media",
        "查询 PNKX 博客内容：文章、相册、视频、留言板、友链。",
        """用户问博客文章、相册、视频、留言时调用对应列表与详情；热门文章与按类型分组用于“最近写了什么”类问题。这些多为公开内容，回答时注明来自博客。GET 带副作用的接口（浏览+1、点赞）不在契约内。""",
        [
            op("article_list", "分页查询文章（含内容）", "GET", "/client/article/list", "read",
               merge({"title": q(), "tag": q(), "type": q(), "search": q()}, PAGE)),
            op("article_list_not_content", "分页查询文章（不含正文）", "GET",
               "/client/article/listNotContent", "read",
               merge({"title": q(), "type": q()}, PAGE)),
            op("article_hot", "推荐/热门文章", "GET", "/client/article/getHotArticle", "read"),
            op("article_group_by_type", "文章按类型分组", "GET", "/client/article/getArticleListGroupByType", "read"),
            op("article_detail", "文章详情", "GET", "/client/article/{id}", "read", {"id": p("string")}),
            op("photo_list", "分页查询相册", "GET", "/client/photo/list", "read",
               merge({"name": q(), "type": q()}, PAGE)),
            op("photo_detail", "相册详情", "GET", "/client/photo/{id}", "read", {"id": p("string")}),
            op("video_list", "分页查询视频", "GET", "/client/video/list", "read",
               merge({"title": q(), "label": q()}, PAGE)),
            op("video_detail", "视频详情", "GET", "/client/video/{id}", "read", {"id": p()}),
            op("message_list", "分页查询留言板", "GET", "/client/message/getMessageList", "read",
               merge({"authorName": q(), "state": q()}, PAGE)),
            op("friend_link_list", "友情链接列表（不分页）", "GET", "/client/link/list", "read"),
            op("share_resource_list", "分页查询分享资源", "GET", "/client/share/list", "read",
               merge({"title": q(), "resourceType": q()}, PAGE)),
        ],
    ),
]


def render_package(
    name: str, connection: str, description: str, instructions: str, operations: list[dict]
) -> tuple[str, str]:
    manifest = {
        "schema_version": 1,
        "connection": connection,
        "auth": AUTH,
        "operations": operations,
    }
    markdown = f"---\nname: {name}\ndescription: {description}\n---\n{instructions}\n"
    api_text = yaml.dump(manifest, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return markdown, api_text


def build_packages(validate: bool = True) -> dict[str, bytes]:
    zips: dict[str, bytes] = {}
    for name, connection, description, instructions, operations in SKILLS:
        markdown, api_text = render_package(name, connection, description, instructions, operations)
        target = OUT / name
        if "--write" in sys.argv:
            target.mkdir(parents=True, exist_ok=True)
            (target / "SKILL.md").write_text(markdown, encoding="utf-8")
            (target / "aria-api.yaml").write_text(api_text, encoding="utf-8")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{name}/SKILL.md", markdown)
            archive.writestr(f"{name}/aria-api.yaml", api_text)
        data = buffer.getvalue()
        if validate:
            document, _digest = import_skill_zip(data)
            assert isinstance(document, SkillDocument)
            count = len(document.api.operations) if document.api else 0
            assert count == len(operations)
        zips[name] = data
    return zips


if __name__ == "__main__":
    packages = build_packages()
    print(f"validated {len(packages)} skill packages")
    if "--write" in sys.argv:
        print(f"written to {OUT}")
