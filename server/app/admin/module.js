const modules = {
  overview: {
    eyebrow: "ADMIN CONSOLE", title: "后台总览", state: "基础能力已就绪", ready: true,
    description: "查看当前已经交付的能力与后续模块状态。规划中的页面会明确标注，不再出现无反馈的导航。",
    stats: [["服务状态", "检测中", "读取 /healthz"], ["模型配置", "已上线", "数据库版本化"], ["Persona", "已上线", "草稿、发布与回滚"], ["下一阶段", "P2 记忆", "可检索、可纠错"]],
    capabilities: [["模型与路由", "配置模型端点、隐私路由和故障降级。", "/admin/models"], ["Persona", "维护人格、表达风格、边界和 Live2D 表情映射。", "/admin/personas"], ["结构化回复", "字幕、TTS、情绪和动作已进入统一协议。"]],
    roadmap: ["实现记忆系统 v1 与质量指标", "补齐跨存储删除闭环", "接入设备与正式可观测页面"],
    dependencies: "后续模块按照 TASKS.md 的 P2～P6 顺序推进。当前页面只展示真实交付状态。", action: ["进入模型配置", "/admin/models"]
  },
  memory: {
    eyebrow: "MEMORY QUALITY", title: "记忆质量", state: "P2 · 规划中",
    description: "长期记忆的检索、溯源、纠错和质量评估入口。",
    stats: [["阶段", "P2", "下一开发批次"], ["存储", "待接入", "PostgreSQL + pgvector"], ["可溯源", "设计完成", "来源与时间证据"], ["删除闭环", "P3", "跨存储清理"]],
    capabilities: [["会话消息", "聊天正文已在 PostgreSQL 持久化，可作为记忆提取来源。"], ["隐私等级", "L0～L3 分级已建立，记忆写入会复用隐私闸门。"]],
    roadmap: ["定义记忆记录、证据和版本模型", "实现提取、检索与引用链", "增加纠错、遗忘和质量评估后台"],
    dependencies: "依赖稳定的结构化回复与聊天身份；这两项已经完成。", action: ["查看 Persona", "/admin/personas"]
  },
  devices: {
    eyebrow: "DEVICE REGISTRY", title: "设备", state: "P6 · 规划中",
    description: "管理麦克风、扬声器、Live2D、传感器和未来硬件端点。",
    stats: [["适配器注册", "已具备", "统一能力声明"], ["Open-LLM-VTuber", "已接入", "ASR / TTS / Live2D"], ["硬件配对", "待实现", "设备身份与授权"], ["ESP32", "后续", "不阻塞核心体验"]],
    capabilities: [["Adapter Registry", "输入输出适配器具有统一生命周期和能力协商。"], ["Open-LLM-VTuber", "当前可以作为语音与 Live2D 输出端使用。", "http://127.0.0.1:12393/"]],
    roadmap: ["设计设备身份和配对流程", "实现端点健康、能力和权限页面", "增加音频租约与多端仲裁"],
    dependencies: "设备产品化排在文字稳定性闸门之后，避免同时调试过多变量。", action: ["打开 Open-LLM-VTuber", "http://127.0.0.1:12393/"]
  },
  logs: {
    eyebrow: "OBSERVABILITY", title: "日志追踪", state: "基础观测已具备",
    description: "追踪请求、模型路由、回合状态与适配器投递；可视化查询仍在规划中。",
    stats: [["健康检查", "在线", "/healthz"], ["回合状态", "已记录", "accepted → completed"], ["模型元数据", "已记录", "provider / model / latency"], ["日志检索", "待实现", "过滤与关联查询"]],
    capabilities: [["结构化元数据", "消息记录保留模型、配置、Persona、耗时与 token 用量。"], ["服务健康", "配置错误和 dispatcher 状态可进入健康检查。"]],
    roadmap: ["增加 trace 与 correlation 查询 API", "实现回合和模型调用筛选", "补齐错误详情、指标趋势与告警入口"],
    dependencies: "可视化查询需要先冻结日志保留策略，避免后台依赖不稳定字段。", action: ["查看模型路由", "/admin/models"]
  },
  privacy: {
    eyebrow: "PRIVACY AUDIT", title: "隐私审计", state: "核心闸门已启用", ready: true,
    description: "展示 L0～L3 分类、模型出站决策和数据持久化边界。审计查询页面仍待补齐。",
    stats: [["L3 持久化", "强制阻断", "原始传感器不落库"], ["L2 云端出站", "强制阻断", "只允许本地模型"], ["模型密钥", "服务端", "输出端不可见"], ["审计查询", "待实现", "P3 管理闭环"]],
    capabilities: [["入口重分类", "服务端策略可以上调设备声明的隐私等级。"], ["模型出站闸门", "L2 强制 private 路由，L3 不进入持久聊天。"], ["敏感字段脱敏", "结构化日志会清理正文、提示词和嵌套文本。"]],
    roadmap: ["建立审计事件查询接口", "增加按用户、会话和原因筛选", "接入跨存储删除证明"],
    dependencies: "审计读取必须与聊天内容权限隔离，不能让管理 Token 直接读取正文。", action: ["查看模型隐私路由", "/admin/models"]
  },
  settings: {
    eyebrow: "SYSTEM SETTINGS", title: "系统配置", state: "部分能力已上线",
    description: "汇总服务运行参数、身份策略和配置版本。敏感环境变量不会在浏览器中显示。",
    stats: [["模型配置", "数据库", "版本化发布"], ["Persona", "数据库", "版本化发布"], ["聊天会话", "8 小时", "可撤销 Token"], ["通用设置", "待实现", "统一配置入口"]],
    capabilities: [["模型配置版本", "草稿、校验、发布和回滚已经可用。", "/admin/models"], ["Persona 版本", "人格设置拥有独立发布指针。", "/admin/personas"], ["密钥边界", "后台只管理 env: 引用，不保存供应商明文密钥。"]],
    roadmap: ["定义可在线修改与必须重启的设置边界", "增加身份、会话和保留周期设置", "实现配置导入、导出与变更审计"],
    dependencies: "不会把数据库密码、模型 API Key 或聊天密码回显到管理页面。", action: ["进入模型配置", "/admin/models"]
  }
};

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
const pathPart = location.pathname.split("/").filter(Boolean).pop();
const key = location.pathname === "/admin" || location.pathname === "/admin/" ? "overview" : pathPart;
const moduleConfig = modules[key] || modules.overview;
document.title = `Aria · ${moduleConfig.title}`;
document.querySelector("#eyebrow").textContent = moduleConfig.eyebrow;
document.querySelector("#title").textContent = moduleConfig.title;
document.querySelector("#description").textContent = moduleConfig.description;
const state = document.querySelector("#module-state");
state.textContent = moduleConfig.state;
state.classList.toggle("ready", Boolean(moduleConfig.ready));
document.querySelector("#stats").innerHTML = moduleConfig.stats.map(([label, value, detail]) => `<article><span>${escapeHtml(label)}</span><strong class="compact">${escapeHtml(value)}</strong><small>${escapeHtml(detail)}</small></article>`).join("");
document.querySelector("#capabilities").innerHTML = moduleConfig.capabilities.map(([title, detail, href]) => `<div class="capability${href ? "" : " planned"}"><span class="capability-mark">${href ? "✓" : "·"}</span><div><strong>${href ? `<a href="${escapeHtml(href)}">${escapeHtml(title)}</a>` : escapeHtml(title)}</strong><small>${escapeHtml(detail)}</small></div></div>`).join("");
document.querySelector("#roadmap").innerHTML = moduleConfig.roadmap.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
document.querySelector("#dependencies").textContent = moduleConfig.dependencies;
const action = document.querySelector("#primary-action");
action.textContent = moduleConfig.action[0];
action.href = moduleConfig.action[1];
const activeNav = document.querySelector(`[data-nav="${key}"]`);
if (activeNav) { activeNav.classList.add("active"); activeNav.setAttribute("aria-current", "page"); }

fetch("/healthz").then((response) => response.json()).then((health) => {
  document.querySelector("#sidebar-status").textContent = health.status === "ok" ? "后台服务运行正常" : "后台服务状态降级";
  if (key === "overview") {
    const first = document.querySelector("#stats article strong");
    first.textContent = health.status === "ok" ? "正常" : "降级";
    first.classList.add("safe");
  }
}).catch(() => { document.querySelector("#sidebar-status").textContent = "后台服务无法连接"; });
