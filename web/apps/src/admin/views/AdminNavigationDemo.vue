<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";

type DemoTab = {
  key: string;
  label: string;
  description: string;
  features: string[];
};

type DemoModule = {
  key: string;
  label: string;
  group: string;
  eyebrow: string;
  description: string;
  tabs: DemoTab[];
};

const modules: DemoModule[] = [
  {
    key: "overview",
    label: "总览",
    group: "概览",
    eyebrow: "SYSTEM OVERVIEW",
    description: "从异常和待办开始，快速判断系统今天是否健康。",
    tabs: [
      { key: "runtime", label: "运行概览", description: "汇总今天最需要关注的系统状态。", features: ["调用量、成功率与延迟", "设备和模型在线状态", "隐私守护与删除任务状态"] },
      { key: "health", label: "健康与告警", description: "集中处理服务异常与风险告警。", features: ["P0～P3 告警分级", "告警确认与恢复记录", "关联 Trace 快速下钻"] },
      { key: "usage", label: "使用与成本", description: "查看资源消耗、预算与趋势。", features: ["Token 与语音用量", "模型和任务成本归因", "预算阈值与预测"] },
      { key: "activity", label: "最近活动", description: "查看影响系统行为的重要变更。", features: ["配置发布与回滚", "设备配对与撤销", "删除、导出和恢复操作"] },
    ],
  },
  {
    key: "models",
    label: "模型与路由",
    group: "能力接入",
    eyebrow: "MODEL CONTROL",
    description: "管理模型服务、能力槽位和不同隐私等级的调用路径。",
    tabs: [
      { key: "services", label: "模型服务", description: "维护所有本地与云端模型端点。", features: ["Provider、模型和 Base URL", "密钥引用与隐私上限", "超时、重试和上下文参数"] },
      { key: "routing", label: "路由策略", description: "配置 Dialogue、Utility 和 Private 路由。", features: ["主模型与降级链", "路由超时和失败策略", "L2 本地路由保护"] },
      { key: "capabilities", label: "能力配置", description: "为不同任务选择独立能力模型。", features: ["视觉、生图和视频槽位", "Function Calling 支持", "能力可用性检查"] },
      { key: "voice", label: "语音服务", description: "维护 ASR、TTS 和音色降级链。", features: ["ASR Provider 与语种", "TTS 音色和格式", "逐句降级与冷却策略"] },
      { key: "diagnostics", label: "连接诊断", description: "使用合成请求检查服务连通性。", features: ["模型存在与加载状态", "最小化安全探针", "最近连接失败记录"] },
    ],
  },
  {
    key: "persona",
    label: "人格与表达",
    group: "伴侣核心",
    eyebrow: "PERSONA & EXPRESSION",
    description: "定义 Aria 如何理解关系、表达情绪并守住边界。",
    tabs: [
      { key: "profile", label: "基本设定", description: "维护身份、称呼和关系定位。", features: ["角色名称与背景", "用户称呼和关系阶段", "基线情绪与主动程度"] },
      { key: "style", label: "表达风格", description: "控制语言风格和回复节奏。", features: ["语气、用词和回复长度", "不同场景表达偏好", "语音表达提示"] },
      { key: "boundaries", label: "边界规则", description: "明确安全、伦理与交往边界。", features: ["敏感话题处理", "依赖和危机边界", "禁止行为与升级策略"] },
      { key: "motion", label: "动作映射", description: "连接情绪、语音与角色动作。", features: ["情绪到表情映射", "动作强度与冷却", "Live2D 能力适配"] },
      { key: "versions", label: "版本历史", description: "管理草稿、发布和回滚。", features: ["版本对比", "发布前校验", "一键创建回滚版本"] },
    ],
  },
  {
    key: "memory",
    label: "记忆与历史",
    group: "伴侣核心",
    eyebrow: "MEMORY & HISTORY",
    description: "把记忆内容、质量治理和时间线放到同一个工作区。",
    tabs: [
      { key: "library", label: "记忆库", description: "查询、纠正和管理长期记忆。", features: ["按主体、类型和隐私筛选", "来源证据与有效期", "新增、纠正和失效"] },
      { key: "quality", label: "质量分析", description: "评估记忆是否准确并被正确引用。", features: ["准确率与错误引用率", "检索得分和采用情况", "回归集运行结果"] },
      { key: "conflicts", label: "冲突处理", description: "人工处理相互矛盾的新旧事实。", features: ["新旧事实并排比较", "替代、并存或拒绝", "处理理由与审计记录"] },
      { key: "timeline", label: "历史时间线", description: "按时间回看真正发生过的重要事件。", features: ["按日期、主体和类型筛选", "事件与来源关联", "L2 内容受控下钻"] },
      { key: "deletion", label: "删除验证", description: "证明删除已经覆盖所有存储层。", features: ["消息、记忆和向量状态", "缓存与备份删除台账", "残留扫描和重放结果"] },
    ],
  },
  {
    key: "devices",
    label: "设备与授权",
    group: "能力接入",
    eyebrow: "DEVICES & ACCESS",
    description: "管理设备身份、能力、权限和运行状态。",
    tabs: [
      { key: "registry", label: "设备列表", description: "查看已经接入 Aria 的全部设备。", features: ["在线状态与最后心跳", "设备类型和客户端版本", "当前权限摘要"] },
      { key: "pairing", label: "配对授权", description: "处理设备接入、授权和撤销。", features: ["短期配对码", "权限范围确认", "设备身份撤销"] },
      { key: "channels", label: "能力与通道", description: "检查设备能够提供或消费的能力。", features: ["屏幕、浏览器与音频", "传感和输出通道", "服务端隐私重定级"] },
      { key: "commands", label: "命令记录", description: "追踪发送到设备的指令和结果。", features: ["命令状态与耗时", "目标解析过程", "失败原因与重试"] },
      { key: "diagnostics", label: "健康诊断", description: "发现离线、抖动和能力异常。", features: ["网络与心跳质量", "消息速率异常", "时间漂移与版本问题"] },
    ],
  },
  {
    key: "logs",
    label: "日志追踪",
    group: "运维治理",
    eyebrow: "OBSERVABILITY",
    description: "围绕一次交互定位失败、延迟、重试和降级。",
    tabs: [
      { key: "traces", label: "Trace 查询", description: "按一次完整调用链进行排障。", features: ["trace/correlation ID 查询", "Span 时间轴", "重试、降级和隐私决策"] },
      { key: "events", label: "事件日志", description: "检索经过脱敏的结构化事件。", features: ["时间、服务和事件筛选", "等级、状态和错误码", "无正文结构化字段"] },
      { key: "performance", label: "性能分析", description: "发现模型和语音链路的性能退化。", features: ["P50/P90 延迟", "首字与首音频耗时", "阶段瀑布与趋势"] },
      { key: "errors", label: "错误分析", description: "聚合同类失败并定位共同根因。", features: ["错误码和服务聚合", "失败趋势与影响范围", "关联 Trace 样本"] },
      { key: "alerts", label: "告警记录", description: "管理告警生命周期。", features: ["产生、确认和恢复", "去重与冷却", "关联根因和处置记录"] },
    ],
  },
  {
    key: "privacy",
    label: "隐私与安全",
    group: "运维治理",
    eyebrow: "PRIVACY & SECURITY",
    description: "证明数据在采集、存储、外发和删除阶段都受到约束。",
    tabs: [
      { key: "summary", label: "隐私概览", description: "集中显示隐私状态和高风险异常。", features: ["L0～L3 处理分布", "审计完整率", "L3 Canary 与 P0 状态"] },
      { key: "flow", label: "数据流向", description: "按生命周期查看数据如何被处理。", features: ["采集、定级与持久化", "检索、导出与删除", "目的地和规则摘要"] },
      { key: "egress", label: "外发审计", description: "检查发送给外部 Provider 的字段。", features: ["Provider 与隐私等级", "允许、拒绝和原因码", "Payload 不可逆摘要"] },
      { key: "operations", label: "操作审计", description: "记录所有高风险管理行为。", features: ["查看、导出和删除", "恢复与配置变更", "操作者、目标和结果"] },
      { key: "policies", label: "策略与验证", description: "维护隐私策略并运行验证。", features: ["Egress 与存储规则", "删除证明", "重新认证和变更审计"] },
    ],
  },
  {
    key: "system",
    label: "系统与维护",
    group: "运维治理",
    eyebrow: "SYSTEM & MAINTENANCE",
    description: "管理全局运行环境、身份策略、备份和升级。",
    tabs: [
      { key: "general", label: "基础设置", description: "维护实例级通用设置。", features: ["实例名称、语言和时区", "默认城市和区域", "在线修改与重启标识"] },
      { key: "identity", label: "身份与会话", description: "管理后台与聊天身份边界。", features: ["管理员认证策略", "会话时长与撤销", "设备身份和访问范围"] },
      { key: "observability", label: "可观测性", description: "配置日志、Trace 和告警策略。", features: ["日志等级与 Trace 采样", "数据保留周期", "告警阈值和通知"] },
      { key: "storage", label: "存储与备份", description: "管理容量、备份和恢复验证。", features: ["存储用量与配额", "备份计划和加密", "隔离恢复演练"] },
      { key: "updates", label: "升级与信息", description: "查看版本并安全完成系统升级。", features: ["应用与数据库版本", "兼容性和迁移检查", "更新、回退和 License"] },
    ],
  },
];

const groupOrder = ["概览", "伴侣核心", "能力接入", "运维治理"];
const activeModuleKey = ref("overview");
const activeTabKey = ref("runtime");

const activeModule = computed(() => modules.find((item) => item.key === activeModuleKey.value) ?? modules[0]);
const activeTab = computed(() => activeModule.value.tabs.find((item) => item.key === activeTabKey.value) ?? activeModule.value.tabs[0]);
const groupedModules = computed(() => groupOrder.map((group) => ({ group, items: modules.filter((item) => item.group === group) })));

const metricSets: Record<string, [string, string, string][]> = {
  overview: [["今日调用", "3,247", "较昨日 +18.7%"], ["请求成功率", "99.2%", "目标 ≥ 98%"], ["待处理告警", "2", "无 P0 告警"]],
  models: [["可用模型", "6 / 7", "1 个需要检查"], ["主路由延迟", "612 ms", "P90 824 ms"], ["本月成本", "¥ 18.72", "预算使用 31%"]],
  persona: [["当前版本", "v12", "发布于 2 天前"], ["待发布草稿", "1", "校验已通过"], ["动作映射", "18", "覆盖 6 种情绪"]],
  memory: [["有效记忆", "1,284", "L2 占比 14%"], ["引用准确率", "92.6%", "近 7 天 +2.4%"], ["待处理冲突", "7", "最早 3 天前"]],
  devices: [["在线设备", "4 / 5", "1 台离线"], ["已授权能力", "12", "2 项敏感能力"], ["命令成功率", "98.7%", "过去 24 小时"]],
  logs: [["成功链路", "99.2%", "过去 24 小时"], ["P90 延迟", "824 ms", "目标 < 1.5s"], ["未确认告警", "2", "最高 P2"]],
  privacy: [["L3 外泄", "0", "隐私闸门正常"], ["审计完整率", "100%", "关键操作全覆盖"], ["外发拒绝", "23", "过去 24 小时"]],
  system: [["系统版本", "0.6.0", "当前为最新"], ["备份状态", "正常", "8 小时前完成"], ["存储使用", "38%", "剩余 62 GB"]],
};

const metrics = computed(() => metricSets[activeModule.value.key] ?? metricSets.overview);

function selectModule(module: DemoModule) {
  activeModuleKey.value = module.key;
  activeTabKey.value = module.tabs[0].key;
}

function syncFromHash() {
  const params = new URLSearchParams(location.hash.replace(/^#/, ""));
  const requestedModule = modules.find((item) => item.key === params.get("module"));
  if (!requestedModule) return;
  activeModuleKey.value = requestedModule.key;
  activeTabKey.value = requestedModule.tabs.some((tab) => tab.key === params.get("tab"))
    ? String(params.get("tab"))
    : requestedModule.tabs[0].key;
}

onMounted(() => {
  syncFromHash();
  window.addEventListener("hashchange", syncFromHash);
});

watch([activeModuleKey, activeTabKey], () => {
  const next = `module=${activeModuleKey.value}&tab=${activeTabKey.value}`;
  if (location.hash.slice(1) !== next) history.replaceState(null, "", `#${next}`);
});
</script>

<template>
  <div class="demo-shell">
    <aside class="demo-sidebar">
      <div class="demo-brand">
        <span class="demo-brand-mark">A</span>
        <div><strong>Aria Hub</strong><small>管理后台 Demo</small></div>
      </div>

      <nav class="demo-nav" aria-label="管理后台导航">
        <section v-for="group in groupedModules" :key="group.group" class="demo-nav-group">
          <p>{{ group.group }}</p>
          <button
            v-for="item in group.items"
            :key="item.key"
            type="button"
            :class="{ active: item.key === activeModule.key }"
            @click="selectModule(item)"
          >
            <span>{{ item.label }}</span>
            <small v-if="item.key === 'privacy'" aria-hidden="true">安全</small>
          </button>
        </section>
      </nav>

      <div class="demo-connection"><span></span><div><strong>本地连接正常</strong><small>数据仅保存在本设备</small></div></div>
    </aside>

    <main class="demo-main">
      <header class="demo-topbar">
        <div>
          <p>{{ activeModule.eyebrow }}</p>
          <h1>{{ activeModule.label }}</h1>
        </div>
        <div class="demo-top-actions">
          <span class="demo-updated">更新于 10:32</span>
          <el-button>刷新</el-button>
          <el-button type="primary">主要操作</el-button>
        </div>
      </header>

      <div class="demo-workspace">
        <p class="demo-description">{{ activeModule.description }}</p>

        <div class="demo-tabs" role="tablist" :aria-label="`${activeModule.label}页面`">
          <button
            v-for="tab in activeModule.tabs"
            :key="tab.key"
            type="button"
            role="tab"
            :aria-selected="tab.key === activeTab.key"
            :class="{ active: tab.key === activeTab.key }"
            @click="activeTabKey = tab.key"
          >{{ tab.label }}</button>
        </div>

        <section class="demo-metrics" aria-label="关键指标">
          <article v-for="metric in metrics" :key="metric[0]">
            <span>{{ metric[0] }}</span>
            <strong>{{ metric[1] }}</strong>
            <small>{{ metric[2] }}</small>
          </article>
        </section>

        <section class="demo-panel">
          <div class="demo-panel-heading">
            <div><span>当前 Tab</span><h2>{{ activeTab.label }}</h2><p>{{ activeTab.description }}</p></div>
            <el-tag type="success" effect="plain">Demo 数据</el-tag>
          </div>

          <div class="demo-content-grid">
            <article class="demo-feature-card">
              <h3>本页包含</h3>
              <ul>
                <li v-for="(feature, index) in activeTab.features" :key="feature">
                  <span>{{ String(index + 1).padStart(2, "0") }}</span>
                  <div><strong>{{ feature }}</strong><small>支持筛选、查看详情与关联下钻</small></div>
                </li>
              </ul>
            </article>

            <article class="demo-state-card">
              <h3>页面状态</h3>
              <dl>
                <div><dt>默认权限</dt><dd>{{ activeModule.key === "overview" ? "viewer" : activeModule.key === "logs" || activeModule.key === "devices" ? "operator" : "admin" }}</dd></div>
                <div><dt>当前范围</dt><dd>过去 24 小时</dd></div>
                <div><dt>内容策略</dt><dd>{{ activeModule.key === "logs" || activeModule.key === "privacy" ? "不读取业务正文" : "按权限受控显示" }}</dd></div>
                <div><dt>URL 状态</dt><dd>可复制与恢复</dd></div>
              </dl>
              <button type="button" class="demo-text-button">查看产品说明</button>
            </article>
          </div>
        </section>
      </div>
    </main>
  </div>
</template>

<style scoped>
.demo-shell { min-height: 100dvh; display: grid; grid-template-columns: 232px minmax(0, 1fr); background: #f6f8fc; color: #172033; }
.demo-sidebar { position: sticky; top: 0; height: 100dvh; display: flex; flex-direction: column; padding: 20px 14px 16px; background: #fff; border-right: 1px solid #e3e8f2; overflow-y: auto; }
.demo-brand { display: flex; align-items: center; gap: 11px; padding: 0 8px 19px; border-bottom: 1px solid #edf0f6; }
.demo-brand-mark { display: grid; place-items: center; width: 34px; height: 34px; border-radius: 10px; background: #4f6df5; color: #fff; font-weight: 700; box-shadow: 0 6px 14px rgba(79,109,245,.22); }
.demo-brand div { display: grid; gap: 2px; }
.demo-brand strong { font-size: 14px; }
.demo-brand small { color: #8a95a8; font-size: 11px; }
.demo-nav { padding-top: 12px; }
.demo-nav-group { margin-bottom: 12px; }
.demo-nav-group > p { margin: 0 10px 5px; color: #9aa4b5; font-size: 10px; font-weight: 700; letter-spacing: .12em; }
.demo-nav-group button { width: 100%; height: 38px; display: flex; align-items: center; justify-content: space-between; border: 0; border-radius: 8px; padding: 0 11px; background: transparent; color: #66738a; font: inherit; font-size: 13px; cursor: pointer; text-align: left; }
.demo-nav-group button:hover { background: #f7f9fd; color: #172033; }
.demo-nav-group button.active { background: #eef2ff; color: #3658e8; font-weight: 600; }
.demo-nav-group button small { padding: 2px 5px; border-radius: 5px; background: #e8f7ef; color: #218958; font-size: 9px; }
.demo-connection { margin-top: auto; display: flex; gap: 9px; align-items: flex-start; padding: 12px; border: 1px solid #e6ebf3; border-radius: 10px; background: #fafbfe; }
.demo-connection > span { width: 8px; height: 8px; margin-top: 4px; border-radius: 50%; background: #26b873; box-shadow: 0 0 0 3px #e5f8ef; }
.demo-connection div { display: grid; gap: 3px; }
.demo-connection strong { font-size: 11px; }
.demo-connection small { color: #8792a5; font-size: 10px; }
.demo-main { min-width: 0; }
.demo-topbar { height: 72px; display: flex; align-items: center; justify-content: space-between; padding: 0 22px; background: rgba(255,255,255,.94); border-bottom: 1px solid #e3e8f2; }
.demo-topbar p { margin: 0 0 4px; color: #8c98aa; font-size: 10px; font-weight: 700; letter-spacing: .12em; }
.demo-topbar h1 { margin: 0; font-size: 21px; letter-spacing: -.02em; }
.demo-top-actions { display: flex; align-items: center; gap: 9px; }
.demo-updated { color: #8b96a8; font-size: 12px; margin-right: 4px; }
.demo-workspace { width: 100%; padding: 18px 22px 32px; }
.demo-description { margin: 0 0 18px; color: #68748a; font-size: 13px; }
.demo-tabs { display: flex; gap: 4px; padding: 4px; width: fit-content; max-width: 100%; overflow-x: auto; border: 1px solid #e1e6f0; border-radius: 10px; background: #fff; box-shadow: 0 3px 10px rgba(34,49,82,.04); }
.demo-tabs button { flex: none; height: 34px; border: 0; border-radius: 7px; padding: 0 15px; background: transparent; color: #6b768a; font: inherit; font-size: 12px; cursor: pointer; }
.demo-tabs button:hover { color: #314358; background: #f7f9fd; }
.demo-tabs button.active { background: #4f6df5; color: #fff; font-weight: 600; box-shadow: 0 3px 8px rgba(79,109,245,.2); }
.demo-metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-top: 18px; }
.demo-metrics article { min-height: 116px; display: grid; align-content: center; gap: 7px; padding: 20px 22px; border: 1px solid #e1e6f0; border-radius: 12px; background: #fff; box-shadow: 0 5px 18px rgba(33,47,77,.035); }
.demo-metrics span { color: #778297; font-size: 12px; }
.demo-metrics strong { font-size: 27px; letter-spacing: -.03em; }
.demo-metrics small { color: #26a76b; font-size: 11px; }
.demo-panel { margin-top: 14px; border: 1px solid #e1e6f0; border-radius: 12px; background: #fff; box-shadow: 0 5px 18px rgba(33,47,77,.035); overflow: hidden; }
.demo-panel-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; padding: 22px 24px; border-bottom: 1px solid #edf0f6; }
.demo-panel-heading span { color: #98a2b3; font-size: 10px; font-weight: 700; letter-spacing: .08em; }
.demo-panel-heading h2 { margin: 5px 0 5px; font-size: 17px; }
.demo-panel-heading p { margin: 0; color: #758095; font-size: 12px; }
.demo-content-grid { display: grid; grid-template-columns: minmax(0, 1.65fr) minmax(240px, .8fr); }
.demo-feature-card, .demo-state-card { padding: 22px 24px 25px; }
.demo-feature-card { border-right: 1px solid #edf0f6; }
.demo-feature-card h3, .demo-state-card h3 { margin: 0 0 17px; font-size: 13px; }
.demo-feature-card ul { list-style: none; margin: 0; padding: 0; }
.demo-feature-card li { display: flex; align-items: center; gap: 14px; padding: 14px 0; border-top: 1px solid #f0f2f7; }
.demo-feature-card li:first-child { border-top: 0; padding-top: 2px; }
.demo-feature-card li > span { display: grid; place-items: center; width: 29px; height: 29px; border-radius: 8px; background: #f0f3ff; color: #4f6df5; font-size: 10px; font-weight: 700; }
.demo-feature-card li div { display: grid; gap: 4px; }
.demo-feature-card li strong { font-size: 12px; }
.demo-feature-card li small { color: #8b96a8; font-size: 10px; }
.demo-state-card dl { margin: 0; }
.demo-state-card dl div { display: flex; justify-content: space-between; gap: 16px; padding: 11px 0; border-bottom: 1px solid #f0f2f7; font-size: 11px; }
.demo-state-card dt { color: #8792a5; }
.demo-state-card dd { margin: 0; color: #354158; font-weight: 600; text-align: right; }
.demo-text-button { margin-top: 18px; border: 0; padding: 0; background: transparent; color: #4f6df5; font: inherit; font-size: 11px; font-weight: 600; cursor: pointer; }
@media (max-width: 900px) {
  .demo-shell { grid-template-columns: 190px minmax(0, 1fr); }
  .demo-workspace { padding-inline: 18px; }
  .demo-topbar { padding-inline: 18px; }
  .demo-metrics { grid-template-columns: 1fr; }
}
@media (max-width: 680px) {
  .demo-shell { display: block; }
  .demo-sidebar { position: static; height: auto; padding-bottom: 10px; }
  .demo-nav { display: flex; gap: 8px; overflow-x: auto; }
  .demo-nav-group { flex: none; margin: 0; }
  .demo-nav-group > p, .demo-connection { display: none; }
  .demo-nav-group button { width: auto; }
  .demo-brand { border-bottom: 0; padding-bottom: 10px; }
  .demo-topbar { height: auto; align-items: flex-start; padding-block: 16px; }
  .demo-updated, .demo-top-actions .el-button:first-of-type { display: none; }
  .demo-workspace { padding: 18px 14px 30px; }
  .demo-content-grid { grid-template-columns: 1fr; }
  .demo-feature-card { border-right: 0; border-bottom: 1px solid #edf0f6; }
}
</style>
