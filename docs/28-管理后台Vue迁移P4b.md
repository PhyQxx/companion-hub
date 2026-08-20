# P4b 管理后台 Vue 迁移验收记录

> 文档类型：Phase Record / Historical
> 当前后台设计真源：`05-管理后台与可观测性.md`；前端技术决策见 ADR-018（docs/26）。

> 日期：2026-08-18
> 范围：`web/apps/admin` 管理后台 SPA、FastAPI 双模式托管、迁移修复与浏览器实测
> 关联：ADR-018（docs/26）、P4a（docs/27）

## 交付内容

### `web/apps/admin`（Vue 3 + Vue Router + TS）

- 外壳：管理令牌鉴权（sessionStorage，与其他后台共用令牌）、侧栏导航、状态提示；全后台统一浅色主题并引入 Element Plus 作为交互组件库；
- 总览：当前模型配置摘要、当前 Persona、活跃记忆与台账计数 + 快捷入口；
- 模型与路由：左侧模型列表 + 单模型编辑区；基本信息、连接配置、运行参数属于当前模型；全局路由策略和日志/可观测性独立成块；支持完整 JSON 高级编辑；保存语义已从“草稿/发布/版本历史”收敛为“保存并生效”；
- Persona：人格定义完整表单（含表情映射 JSON）+ 草稿/发布/回滚；
- 记忆库：统计、类型/状态/重要性过滤、溯源详情（来源 + 版本链）、纠错编辑、
  归档、冲突裁决（采纳/保留）、硬删除（强制输入原因）、手动添加、检索调试
  （分项得分与命中原因）、删除台账与重放（试运行/执行）；
- 设备/日志/隐私/设置四个占位页给出明确交付批次说明。

### 托管与迁移修复

- FastAPI 双模式：`web/apps/admin/dist` 存在时 `/admin/*` 服务 SPA（含深链回退与
  `/admin/assets`）；纯源码环境回退 vanilla 页面（资源迁至 `/admin/legacy`）。
- **迁移修复**：迁移文件中 `BigInteger` 主键在 sqlite 上不自动递增（ORM 有
  with_variant 而迁移没有），导致 sqlite + alembic 启动失败；已为全部 BIGINT
  主键补 sqlite Integer 变体，sqlite 全链启动验证通过。
- Dockerfile web 构建阶段扩展为 `pnpm -r build`（chat + admin）。

### 浏览器实测（隔离 sqlite 环境）

- `/admin` SPA 加载、令牌连接、总览数据绑定（配置 v1 / Persona Aria / 统计卡）；
- 修复实测发现的两个 bug：路由 base 下的导航链接重复前缀（`/admin/admin/...`）、
  标题判断使用完整路径；
- 深链直达 `/admin/memory` 完整渲染（检索调试、过滤、列表、台账、重放按钮）；
- `/chat` 登录页正确识别 `setup_required` 并切换首次设置表单。

### 注释与文案规范（本批确立）

- 全部 UI 文案使用中文；
- 代码注释统一使用中文并尽量详尽（模块 docstring 说明设计意图、关键分支
  加行内注释）；ruff 豁免扩展到 RUF002/RUF003 以接受中文全角标点；
- 本次已为 memory 包全部模块、chat service 集成段与 web 前端核心文件补齐中文注释。

## 验证

- 后端：pytest 107 通过 / ruff / mypy 全绿；sqlite + alembic 全迁移链启动成功；
- 前端：vue-tsc 类型检查与双应用构建通过（admin 主包 gzip ~39KB）；
- 浏览器：DOM 快照验证鉴权流、路由、数据绑定与深链（截图能力在当前 IAB 环境不可用）。

## P4 收口

P4a + P4b 完成后，P4 全部结束：正式前端（chat + admin）已迁移到 Vue 3，
Open-LLM-VTuber 保持渲染/语音端定位。下一阶段 P5：14 天真实文字使用验证，
验证载体即本次交付的 Vue chat 前端。

## P5 前置联调增量（2026-08-18）

- 管理后台全部菜单统一 Element Plus 组件与浅色视觉主题，移除原生多选、按钮、表格等控件造成的浏览器差异；
- 模型列表显示各端点在 dialogue / utility / private 路由中的实际角色（主模型、备用模型、未参与路由）；
- 模型配置页删除版本概念，`PUT /api/v1/admin/config/current` 保存后立即生效；数据库 revision 仅保留为内部审计实现；
- Persona 发布后的跨 worker 一致性改为聊天新回合主动刷新数据库当前指针；聊天前端通过 `/api/v1/meta/runtime` 显示当前 Persona；
- 当前代码已通过 `git diff --check`。Coding 执行环境缺少 `uv/python/pytest`，因此本轮新增后端回归测试尚未在该环境执行；浏览器端 Persona v2 最终显示仍列为 P5 前置联调待确认项。
