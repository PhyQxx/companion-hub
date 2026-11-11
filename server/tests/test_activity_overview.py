from app.tasks.activity_overview import compose_overview


def test_overview_synthesizes_themes_and_progress() -> None:
    text = """【聊天】
- 讨论接口设计 [timeline:1]
【屏幕】
- 修复代码测试 [timeline:2]
【任务（期间新建或完成；状态为生成时状态）】
- 实现接口 · done [task:1]
- 补文档 · active [task:2]
【日程（计划安排，不表示已参加）】
- 评审 [calendar:1]
"""
    result = compose_overview(text, "weekly")
    assert "这一周" in result and "开发与技术排查" in result
    assert "1 项在生成时已完成，1 项仍待推进" in result
    assert "是否实际参加需另行确认" in result
    assert "[timeline:" not in result


def test_overview_insufficient_and_bounded_evidence() -> None:
    assert "不足以" in compose_overview("无记录。", "daily")
    result = compose_overview("【浏览】\n- 未知活动 [timeline:1]\n仅展示前 100 条", "daily")
    assert "不足以可靠归纳" in result
    assert "仅覆盖已保存的样本" in result
