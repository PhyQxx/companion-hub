# ruff: noqa: RUF001, RUF002, RUF003, E501
"""记忆回归评估集的用例数据（docs/03 §1.8 最小版）。

数量与判定阈值对齐 docs/01 Release Criteria 第 2~4 条：
- 正例 20 条，正确召回 ≥ 16（80%）；
- 负例 20 条（无关 / 已过期 / 已被替换），错误引用 ≤ 1；
- 删除后不可检索；L2 敏感记忆不得出现在 L0/L1 检索。

用例只描述「种什么、问什么、期待什么」，执行语义在 test_memory_eval.py。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PositiveCase:
    """正例：种子记忆 + 查询后，证据级命中应包含该事实。"""

    case_id: str
    content: str
    query: str
    expect_term: str
    fact_key: str | None = None
    subject: str = "user"


@dataclass(frozen=True, slots=True)
class NegativeCase:
    """负例：查询不得把无关 / 过期 / 已替换内容当作证据引用。

    kind 决定种子方式：
    - irrelevant：不种任何相关记忆，检索不得凭空命中语料库其他事实；
    - expired：种下但 valid_to 已过期；
    - superseded：旧值已被 edit 替换，只有新值可引用。
    """

    case_id: str
    kind: str
    content: str
    query: str
    forbidden_term: str


@dataclass(frozen=True, slots=True)
class ConflictCase:
    """冲突：同主体同 fact_key 的新旧值必须显式裁决，不允许静默覆盖。"""

    case_id: str
    fact_key: str
    old_value: str
    new_value: str
    query: str


@dataclass(frozen=True, slots=True)
class DeletionCase:
    """删除：硬删除后任何检索通道都不得再现，且台账留痕。"""

    case_id: str
    content: str
    query: str
    fact_key: str


# ---------------------------------------------------------------------------
# 正例 20 条：覆盖用户事实、助手档案、shared 约定与 fact_key 精确槽位。
# 查询刻意换成口语化说法，避免「照抄原文」的假阳性。
# ---------------------------------------------------------------------------

POSITIVE_CASES: tuple[PositiveCase, ...] = (
    PositiveCase("pos-user-name", "用户的名字是浩宇", "我叫什么名字？", "浩宇", "profile.name"),
    PositiveCase("pos-user-city", "用户生活在杭州", "我住在哪个城市？", "杭州", "profile.city"),
    PositiveCase("pos-user-job", "用户是后端工程师", "我的职业是什么？", "后端", "profile.job"),
    PositiveCase("pos-user-food", "用户不吃香菜，点菜要去掉", "帮我点菜需要注意什么？", "香菜"),
    PositiveCase("pos-user-allergy", "用户对芒果过敏", "我能吃芒果吗？", "过敏", "user.allergy"),
    PositiveCase(
        "pos-user-wakeup", "用户工作日早上七点起床", "我平时几点起床？", "七点", "routine.wakeup"
    ),
    PositiveCase(
        "pos-user-pet", "用户养了一只叫团子的橘猫", "我家养的猫叫什么？", "团子", "profile.pet"
    ),
    PositiveCase("pos-user-movie", "用户最喜欢的电影是星际穿越", "推荐电影可以参考我的喜好", "星际穿越"),
    PositiveCase(
        "pos-user-sport", "用户每周去两次游泳池锻炼", "我这周的运动安排是什么？", "游泳", "routine.sport"
    ),
    PositiveCase(
        "pos-user-anniversary", "用户和助手的纪念日是 5 月 20 日", "我们的纪念日是哪天？", "5 月 20 日"
    ),
    PositiveCase(
        "pos-assistant-height",
        "助手身高为 165 厘米",
        "你多高呀？",
        "165",
        "profile.height",
        subject="assistant",
    ),
    PositiveCase(
        "pos-assistant-weight",
        "助手体重为 50 公斤",
        "你现在多重？",
        "50",
        "profile.weight",
        subject="assistant",
    ),
    PositiveCase(
        "pos-assistant-nickname",
        "助手喜欢被叫做小艾",
        "你的昵称是什么？",
        "小艾",
        "profile.nickname",
        subject="assistant",
    ),
    PositiveCase(
        "pos-assistant-food",
        "助手最喜欢的食物是草莓蛋糕",
        "你最爱吃什么甜点？",
        "草莓蛋糕",
        "preference.food",
        subject="assistant",
    ),
    PositiveCase(
        "pos-assistant-color",
        "助手喜欢的颜色是海蓝色",
        "你喜欢什么颜色？",
        "海蓝色",
        "preference.color",
        subject="assistant",
    ),
    PositiveCase(
        "pos-shared-movie-night",
        "双方约定每周五晚上一起看电影",
        "我们周五晚上的约定是什么？",
        "电影",
        subject="shared",
    ),
    PositiveCase(
        "pos-shared-code", "双方之间用「月见」作为暗号", "我们的暗号是什么？", "月见", subject="shared"
    ),
    PositiveCase(
        "pos-shared-savings", "双方约定每月存两千元旅行基金", "我们的旅行基金每月存多少？", "两千", subject="shared"
    ),
    PositiveCase("pos-user-coffee", "用户只喝美式咖啡，不加糖", "帮我买杯咖啡，记得我的口味", "美式"),
    PositiveCase("pos-user-bike", "用户每天骑电车通勤八点出门", "我几点出门上班？", "八点"),
)


# ---------------------------------------------------------------------------
# 负例 20 条：8 无关 + 6 过期 + 6 已替换。
# forbidden_term 是「若出现在证据级命中里即记一次错误引用」的词。
# ---------------------------------------------------------------------------

NEGATIVE_CASES: tuple[NegativeCase, ...] = (
    NegativeCase("neg-irr-stock", "irrelevant", "—", "我买的哪只股票今天涨了？", "股票"),
    NegativeCase("neg-irr-weather", "irrelevant", "—", "明天下午会下雨吗？", "下雨"),
    NegativeCase("neg-irr-flight", "irrelevant", "—", "我的航班几点起飞？", "航班"),
    NegativeCase("neg-irr-password", "irrelevant", "—", "我的银行卡密码是多少？", "密码"),
    NegativeCase("neg-irr-colleague", "irrelevant", "—", "我的同事小李今天说什么了？", "小李"),
    NegativeCase("neg-irr-package", "irrelevant", "—", "我的快递到哪了？", "快递"),
    NegativeCase("neg-irr-meeting", "irrelevant", "—", "我上周开会讨论了什么？", "开会"),
    NegativeCase("neg-irr-medical", "irrelevant", "—", "我上次体检的结果怎么样？", "体检"),
    NegativeCase(
        "neg-exp-old-job", "expired", "用户之前在宁波做测试工程师", "我以前在哪个城市工作？", "宁波"
    ),
    NegativeCase(
        "neg-exp-old-phone", "expired", "用户以前的手机号是 13800001111", "我以前的手机号是多少？", "13800001111"
    ),
    NegativeCase(
        "neg-exp-gym-card", "expired", "用户的健身房月卡已于上月底到期", "我的健身卡还能用吗？", "到期"
    ),
    NegativeCase(
        "neg-exp-project", "expired", "用户上个月负责的旧项目已经结项", "我现在负责哪个项目？", "结项"
    ),
    NegativeCase(
        "neg-exp-trip", "expired", "用户原定的上周末出游计划已经取消", "我上周末去哪里玩了？", "取消"
    ),
    NegativeCase(
        "neg-exp-subscription", "expired", "用户的视频会员上个月已退订", "我的视频会员还有效吗？", "退订"
    ),
    NegativeCase(
        "neg-sup-old-city", "superseded", "用户生活在宁波", "我现在住在哪个城市？", "宁波"
    ),
    NegativeCase(
        "neg-sup-old-job", "superseded", "用户是前端工程师", "我现在是做什么工作的？", "前端"
    ),
    NegativeCase(
        "neg-sup-old-height", "superseded", "助手身高为 158 厘米", "你多高呀？", "158"
    ),
    NegativeCase(
        "neg-sup-old-nickname", "superseded", "助手以前的名字叫小葵", "你现在叫什么名字？", "小葵"
    ),
    NegativeCase(
        "neg-sup-old-coffee", "superseded", "用户只喝拿铁咖啡", "帮我买杯咖啡，记得我的口味", "拿铁"
    ),
    NegativeCase(
        "neg-sup-old-wakeup", "superseded", "用户工作日早上六点起床", "我平时几点起床？", "六点"
    ),
)


# ---------------------------------------------------------------------------
# 冲突 4 条：裁决前后检索必须只引用胜出方的值。
# ---------------------------------------------------------------------------

CONFLICT_CASES: tuple[ConflictCase, ...] = (
    ConflictCase("conf-weight", "profile.weight", "助手体重为 50 公斤", "助手体重为 52 公斤", "你现在多重？"),
    ConflictCase("conf-color", "preference.color", "助手喜欢的颜色是海蓝色", "助手喜欢的颜色是暖橙色", "你喜欢什么颜色？"),
    ConflictCase(
        "conf-wakeup", "routine.wakeup", "用户工作日早上七点起床", "用户工作日早上六点半起床", "我平时几点起床？"
    ),
    ConflictCase(
        "conf-anniversary", "shared.anniversary", "双方的纪念日是 5 月 20 日", "双方的纪念日是 6 月 18 日", "我们的纪念日是哪天？"
    ),
)


# ---------------------------------------------------------------------------
# 删除 4 条：硬删除后按 fact_key 精确槽位与自然语言查询都必须查不到。
# ---------------------------------------------------------------------------

DELETION_CASES: tuple[DeletionCase, ...] = (
    DeletionCase("del-allergy", "用户对芒果过敏", "我能吃芒果吗？", "user.allergy"),
    DeletionCase("del-pet", "用户养了一只叫团子的橘猫", "我家养的猫叫什么？", "profile.pet"),
    DeletionCase("del-code", "双方之间用「月见」作为暗号", "我们的暗号是什么？", "shared.code"),
    DeletionCase("del-coffee", "用户只喝美式咖啡，不加糖", "帮我买杯咖啡，记得我的口味", "user.coffee"),
)
