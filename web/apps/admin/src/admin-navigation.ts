export interface AdminTab {
  key: string;
  label: string;
  description: string;
  state?: "ready" | "planned";
}

export interface AdminModule {
  key: string;
  path: string;
  label: string;
  group: string;
  tabs: AdminTab[];
}

export const adminModules: AdminModule[] = [
  {
    key: "overview",
    path: "/",
    label: "总览",
    group: "概览",
    tabs: [
      { key: "runtime", label: "运行概览", description: "当前配置、设备、记忆与语音链路状态。", state: "ready" },
      { key: "health", label: "健康与告警", description: "服务健康、异常告警与恢复状态。" },
      { key: "usage", label: "使用与成本", description: "模型、语音与工具调用的用量和成本。" },
      { key: "activity", label: "最近活动", description: "配置、设备、删除和恢复等重要变更。" },
    ],
  },
  {
    key: "persona",
    path: "/personas",
    label: "人格与表达",
    group: "伴侣核心",
    tabs: [
      { key: "profile", label: "基本设定", description: "身份、称呼、关系和档案基线。", state: "ready" },
      { key: "style", label: "表达风格", description: "语言风格、提示词和声音配置。", state: "ready" },
      { key: "proactive", label: "主动发起话题", description: "允许根据屏幕和浏览内容主动发起话题。", state: "ready" },
      { key: "boundaries", label: "边界规则", description: "安全、伦理与互动边界。", state: "ready" },
      { key: "motion", label: "动作映射", description: "情绪、表情和 Live2D 动作映射。", state: "ready" },
      { key: "versions", label: "版本历史", description: "草稿、发布、比较和回滚。", state: "ready" },
    ],
  },
  {
    key: "appearance",
    path: "/appearance",
    label: "形象与外观",
    group: "伴侣核心",
    tabs: [
      { key: "gallery", label: "形象库", description: "形象包、实例管理和人格绑定。", state: "ready" },
      { key: "themes", label: "主题中心", description: "选择账户主题并同步到聊天端。", state: "ready" },
    ],
  },
  {
    key: "memory",
    path: "/memory",
    label: "记忆与历史",
    group: "伴侣核心",
    tabs: [
      { key: "library", label: "记忆库", description: "检索、纠正和管理长期记忆。", state: "ready" },
      { key: "quality", label: "质量分析", description: "准确率、错误引用与回归结果。" },
      { key: "conflicts", label: "冲突处理", description: "集中裁决相互矛盾的新旧事实。" },
      { key: "timeline", label: "历史时间线", description: "按时间回看重要事件和来源证据。", state: "ready" },
      { key: "deletion", label: "删除验证", description: "检查删除台账及跨存储清理状态。", state: "ready" },
    ],
  },
  {
    key: "models",
    path: "/models",
    label: "模型与路由",
    group: "能力接入",
    tabs: [
      { key: "services", label: "模型服务", description: "模型端点、认证和运行参数。", state: "ready" },
      { key: "routing", label: "路由与能力", description: "对话、工具、私密路由和多模态能力槽位。", state: "ready" },
      { key: "tools", label: "工具服务", description: "地图、天气与外部查询能力。", state: "ready" },
      { key: "voice", label: "语音服务", description: "ASR、TTS 和语音降级链。", state: "ready" },
      { key: "home_assistant", label: "HA 连接", description: "Home Assistant 地址、令牌和运行参数。", state: "ready" },
      { key: "mcp", label: "MCP 工具", description: "外部 MCP Server 连接、协议与白名单工具目录。", state: "ready" },
    ],
  },
  {
    key: "devices",
    path: "/devices",
    label: "设备与感知",
    group: "能力接入",
    tabs: [
      { key: "registry", label: "设备列表", description: "设备状态、配对和授权策略。", state: "ready" },
      { key: "pairing", label: "配对授权", description: "配对码、权限确认和身份撤销。" },
      { key: "channels", label: "主动输出通道", description: "控制 Web、Desktop 与 Voice 主动推送策略。", state: "ready" },
      { key: "home_assistant", label: "HA 实体授权", description: "HA 实体发现、读写权限、历史和主动感知。", state: "ready" },
      { key: "status", label: "屏幕感知", description: "周期截屏循环健康与各屏分析进度。", state: "ready" },
      { key: "observations", label: "观察记录", description: "屏幕观察摘要的时间线检索。", state: "ready" },
      { key: "safety", label: "安全守护", description: "安全告警升级链、确认与紧急联系人预授权。", state: "ready" },
      { key: "browser_status", label: "浏览感知", description: "浏览器标签页周期观察循环健康与最近分析。", state: "ready" },
      { key: "browser_observations", label: "浏览观察记录", description: "网页观察摘要的时间线检索。", state: "ready" },
      { key: "commands", label: "命令记录", description: "设备命令、结果和脱敏台账。", state: "ready" },
      { key: "diagnostics", label: "健康诊断", description: "心跳、网络、版本和消息异常。" },
    ],
  },
  {
    key: "logs",
    path: "/logs",
    label: "日志追踪",
    group: "运维治理",
    tabs: [
      { key: "live", label: "实时日志", description: "实时查看后端运行日志流。" },
      { key: "traces", label: "Trace 查询", description: "按调用链定位失败、重试和降级。" },
      { key: "events", label: "事件日志", description: "检索脱敏后的结构化事件。" },
      { key: "performance", label: "性能分析", description: "查看 P50/P90 和阶段耗时。" },
      { key: "errors", label: "错误分析", description: "聚合错误码和共同根因。" },
      { key: "alerts", label: "告警记录", description: "管理告警确认和恢复过程。" },
    ],
  },
  {
    key: "privacy",
    path: "/privacy",
    label: "隐私与安全",
    group: "运维治理",
    tabs: [
      { key: "summary", label: "隐私概览", description: "隐私等级分布、风险与审计完整率。" },
      { key: "flow", label: "数据流向", description: "采集、存储、外发、导出和删除路径。" },
      { key: "egress", label: "外发审计", description: "Provider、字段类型和隐私决策。" },
      { key: "operations", label: "操作审计", description: "查看、导出、恢复和配置变更。" },
      { key: "policies", label: "策略与验证", description: "隐私策略、Canary 和删除证明。" },
    ],
  },
  {
    key: "system",
    path: "/settings",
    label: "系统与维护",
    group: "运维治理",
    tabs: [
      { key: "general", label: "基础设置", description: "实例、语言、时区和区域设置。" },
      { key: "identity", label: "身份与会话", description: "管理员认证、会话和访问边界。" },
      { key: "observability", label: "可观测性", description: "日志、Trace、保留期和告警策略。" },
      { key: "storage", label: "存储与备份", description: "配额、备份和恢复演练。" },
      { key: "jobs", label: "任务中心", description: "后台任务、租约和状态管理。", state: "ready" },
      { key: "updates", label: "升级与信息", description: "版本、迁移、更新和系统信息。" },
    ],
  },
];

export const adminGroups = ["概览", "伴侣核心", "能力接入", "运维治理"].map((group) => ({
  group,
  items: adminModules.filter((item) => item.group === group),
}));

export function findAdminModule(path: string): AdminModule {
  return adminModules.find((item) => item.path === path) ?? adminModules[0];
}
