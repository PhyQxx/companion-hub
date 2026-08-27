<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";
import { ElMessage, ElMessageBox } from "element-plus";

type Action = "turn_on" | "turn_off" | "toggle" | "set_temperature";
type RuleKind =
  | "water_leak"
  | "temperature_high"
  | "temperature_low"
  | "humidity_high"
  | "humidity_low"
  | "pm25_high"
  | "light_on_too_long"
  | "device_offline";

interface ProactiveRule {
  rule_id: string;
  kind: RuleKind;
  enabled: boolean;
  threshold: number | null;
  duration_seconds: number;
  cooldown_minutes: number;
  message: string | null;
}

interface HaEntityPolicy {
  entity_id: string;
  display_name: string;
  aliases: string[];
  read_allowed: boolean;
  history_allowed: boolean;
  history_max_hours: number;
  allowed_actions: Action[];
  confirmation_required_actions: Action[];
  proactive_rules: ProactiveRule[];
  privacy_level: "L0" | "L1" | "L2" | "L3";
  allowed_attributes: string[];
}

interface HaConfig {
  enabled: boolean;
  base_url: string | null;
  secret_ref?: string | null;
  secret_value?: string | null;
  proactive_enabled: boolean;
  proactive_quiet_hours_start: string;
  proactive_quiet_hours_end: string;
  proactive_daily_limit: number;
  proactive_critical_bypasses_quiet_hours: boolean;
  entities: HaEntityPolicy[];
  [key: string]: unknown;
}

interface HubConfig {
  integrations: { home_assistant: HaConfig };
  [key: string]: unknown;
}

interface CurrentConfig {
  version: number;
  config: HubConfig;
}

interface DiscoveredEntity {
  entity_id: string;
  friendly_name: string;
  domain: string;
  state: string;
  device_class?: string | null;
  unit_of_measurement?: string | null;
}

interface EntityDetail {
  entity_id: string;
  friendly_name: string;
  domain: string;
  state: string;
  device_class?: string | null;
  unit_of_measurement?: string | null;
  area?: string | null;
}

interface ConnectionResult {
  ok: boolean;
  latency_ms: number;
  message: string;
  entities: DiscoveredEntity[];
}

const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();
const current = ref<CurrentConfig | null>(null);
const ha = ref<HaConfig | null>(null);
const loading = ref(false);
const saving = ref(false);
const testing = ref(false);
const testingProactive = ref(false);
const testResult = ref<ConnectionResult | null>(null);
const search = ref("");
const selected = ref<string[]>([]);

const HA_ENTITIES_KEY = "aria:ha:entities";

function loadCachedEntities(): EntityDetail[] {
  try {
    const raw = localStorage.getItem(HA_ENTITIES_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed as EntityDetail[];
  } catch {
    return [];
  }
}

function saveCachedEntities(entities: EntityDetail[]) {
  try {
    localStorage.setItem(HA_ENTITIES_KEY, JSON.stringify(entities));
  } catch { /* storage full or private mode */ }
}

const allEntities = ref<EntityDetail[]>(loadCachedEntities());
const fetchingAll = ref(false);
const keyword = ref("");
const areaFilter = ref("");
const domainFilter = ref("");

const areaOptions = computed(() =>
  [...new Set(allEntities.value.map((e) => e.area).filter((a): a is string => !!a))].sort(),
);
const domainOptions = computed(() =>
  [...new Set(allEntities.value.map((e) => e.domain))].sort(),
);
const filteredEntities = computed(() => {
  let rows = allEntities.value;
  const kw = keyword.value.trim().toLowerCase();
  if (kw) {
    rows = rows.filter((item) =>
      [item.entity_id, item.friendly_name, item.domain, item.device_class ?? "", item.area ?? ""]
        .some((v) => v.toLowerCase().includes(kw)),
    );
  }
  if (areaFilter.value) {
    rows = rows.filter((item) => item.area === areaFilter.value);
  }
  if (domainFilter.value) {
    rows = rows.filter((item) => item.domain === domainFilter.value);
  }
  return rows;
});
const authorizedIds = computed(() => new Set(ha.value?.entities.map((e) => e.entity_id) ?? []));

function clonePlain<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

const actionLabels: Record<Action, string> = {
  turn_on: "打开",
  turn_off: "关闭",
  toggle: "切换",
  set_temperature: "设置温度",
};
const ruleLabels: Record<RuleKind, string> = {
  water_leak: "水浸告警",
  temperature_high: "温度过高",
  temperature_low: "温度过低",
  humidity_high: "湿度过高",
  humidity_low: "湿度过低",
  pm25_high: "PM2.5 过高",
  light_on_too_long: "长时间开灯",
  device_offline: "设备离线",
};

const discovered = computed(() => {
  const kw = search.value.trim().toLocaleLowerCase();
  const rows = allEntities.value;
  if (!kw) return rows;
  return rows.filter((item) =>
    [item.entity_id, item.friendly_name, item.domain, item.device_class ?? "", item.area ?? ""]
      .some((value) => value.toLocaleLowerCase().includes(kw)),
  );
});

function normalizeConfig(config: HaConfig): HaConfig {
  return {
    ...config,
    proactive_enabled: config.proactive_enabled ?? false,
    proactive_quiet_hours_start: config.proactive_quiet_hours_start ?? "23:00",
    proactive_quiet_hours_end: config.proactive_quiet_hours_end ?? "07:00",
    proactive_daily_limit: config.proactive_daily_limit ?? 5,
    proactive_critical_bypasses_quiet_hours:
      config.proactive_critical_bypasses_quiet_hours ?? true,
    entities: (config.entities ?? []).map((entity) => {
      const rules = entity.proactive_rules ?? [];
      return {
        ...entity,
        proactive_rules: rules.some((rule) => rule.kind === "device_offline")
          ? rules
          : [...rules, {
              rule_id: "device_offline",
              kind: "device_offline" as const,
              enabled: true,
              threshold: null,
              duration_seconds: 120,
              cooldown_minutes: 240,
              message: null,
            }],
      };
    }),
  };
}

async function load() {
  loading.value = true;
  try {
    current.value = await api.request<CurrentConfig>("/api/v1/admin/config/current");
    ha.value = normalizeConfig(clonePlain(current.value.config.integrations.home_assistant));
    if (ha.value?.enabled && ha.value.base_url && allEntities.value.length === 0) {
      await fetchAllEntities(true);
    }
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "HA 实体配置加载失败", true);
  } finally {
    loading.value = false;
  }
}

async function fetchAllEntities(silent = false) {
  fetchingAll.value = true;
  try {
    const result = await api.request<{
      ok: boolean;
      latency_ms: number;
      message: string;
      entities: EntityDetail[];
    }>("/api/v1/admin/config/integrations/home-assistant/entities", { method: "POST" });
    if (result.ok) {
      allEntities.value = result.entities;
      saveCachedEntities(result.entities);
      if (!silent) ElMessage.success(result.message);
    } else {
      ElMessage.error(result.message);
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "获取 HA 设备列表失败");
  } finally {
    fetchingAll.value = false;
  }
}

async function testConnection() {
  if (!ha.value) return;
  testing.value = true;
  testResult.value = null;
  selected.value = [];
  try {
    const result = await api.request<ConnectionResult>(
      "/api/v1/admin/config/integrations/home-assistant/test",
      { method: "POST", body: JSON.stringify({ config: ha.value }) },
    );
    testResult.value = result;
    result.ok ? ElMessage.success(result.message) : ElMessage.error(result.message);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "HA 连接测试失败");
  } finally {
    testing.value = false;
  }
}

async function testProactive() {
  testingProactive.value = true;
  try {
    const result = await api.request<{ ok: boolean; message: string }>(
      "/api/v1/admin/config/integrations/home-assistant/proactive/test",
      { method: "POST" },
    );
    result.ok ? ElMessage.success(result.message) : ElMessage.warning(result.message);
    emit("status", result.message, !result.ok);
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "测试提醒发送失败", true);
  } finally {
    testingProactive.value = false;
  }
}

async function save() {
  if (!ha.value || !current.value) return;
  saving.value = true;
  try {
    const config = clonePlain(current.value.config);
    config.integrations.home_assistant = {
      ...ha.value,
      entities: ha.value.entities.map((entity) => ({
        ...entity,
        confirmation_required_actions: entity.confirmation_required_actions.filter((action) =>
          entity.allowed_actions.includes(action),
        ),
      })),
    };
    current.value = await api.request<CurrentConfig>("/api/v1/admin/config/current", {
      method: "PUT",
      body: JSON.stringify(config),
    });
    ha.value = normalizeConfig(clonePlain(current.value.config.integrations.home_assistant));
    emit("status", `HA 设备授权已保存，配置版本 ${current.value.version}`);
    ElMessage.success("保存成功，已即时生效");
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "保存失败", true);
  } finally {
    saving.value = false;
  }
}

function supportedActions(entityId: string): Action[] {
  const domain = entityId.split(".", 1)[0];
  if (domain === "light" || domain === "switch") return ["turn_on", "turn_off", "toggle"];
  if (domain === "climate") return ["turn_on", "turn_off", "set_temperature"];
  return [];
}

function supportedRules(entity: HaEntityPolicy): RuleKind[] {
  const key = `${entity.entity_id} ${entity.display_name}`.toLocaleLowerCase();
  const values: RuleKind[] = ["device_offline"];
  if (entity.entity_id.startsWith("light.") || entity.entity_id.startsWith("switch.")) values.unshift("light_on_too_long");
  if (key.includes("water") || key.includes("submersion") || key.includes("水浸") || key.includes("漏水")) values.unshift("water_leak");
  if (key.includes("temperature") || key.includes("温度")) values.unshift("temperature_high", "temperature_low");
  if (key.includes("humidity") || key.includes("湿度")) values.unshift("humidity_high", "humidity_low");
  if (key.includes("pm2") || key.includes("pm25")) values.unshift("pm25_high");
  return [...new Set(values)];
}

function defaultThreshold(kind: RuleKind): number | null {
  return ({ temperature_high: 30, temperature_low: 10, humidity_high: 75, humidity_low: 30, pm25_high: 75 } as Partial<Record<RuleKind, number>>)[kind] ?? null;
}

function addRule(entity: HaEntityPolicy) {
  const existing = new Set(entity.proactive_rules.map((rule) => rule.kind));
  const kind = supportedRules(entity).find((item) => !existing.has(item));
  if (!kind) return ElMessage.info("这个实体的可用规则已全部添加");
  entity.proactive_rules.push({
    rule_id: `${kind}_${entity.proactive_rules.length + 1}`,
    kind,
    enabled: false,
    threshold: defaultThreshold(kind),
    duration_seconds: kind === "water_leak" ? 0 : kind === "light_on_too_long" ? 1800 : 300,
    cooldown_minutes: kind === "water_leak" ? 60 : 240,
    message: null,
  });
}

function onRuleKindChange(rule: ProactiveRule) {
  rule.threshold = defaultThreshold(rule.kind);
  if (rule.kind === "water_leak") rule.duration_seconds = 0;
  if (rule.kind === "light_on_too_long") rule.duration_seconds = 1800;
}

function addSelected() {
  if (!ha.value) return;
  const ids = new Set(selected.value);
  const existing = new Set(ha.value.entities.map((item) => item.entity_id));
  for (const item of allEntities.value) {
    if (!ids.has(item.entity_id) || existing.has(item.entity_id)) continue;
    ha.value.entities.push({
      entity_id: item.entity_id,
      display_name: item.friendly_name,
      aliases: [],
      read_allowed: true,
      history_allowed: false,
      history_max_hours: 24,
      allowed_actions: [],
      confirmation_required_actions: [],
      proactive_rules: [{
        rule_id: "device_offline",
        kind: "device_offline",
        enabled: true,
        threshold: null,
        duration_seconds: 120,
        cooldown_minutes: 240,
        message: null,
      }],
      privacy_level: "L1",
      allowed_attributes: ["friendly_name", "device_class", "unit_of_measurement"],
    });
  }
  selected.value = [];
}

async function removeEntity(index: number) {
  if (!ha.value) return;
  try {
    await ElMessageBox.confirm("移除后中枢将不能读取、控制或主动感知该实体。", "移除 HA 实体", { type: "warning" });
    ha.value.entities.splice(index, 1);
  } catch { /* user cancelled */ }
}

async function clearWhitelist() {
  if (!ha.value) return;
  try {
    await ElMessageBox.confirm("清空后所有实体的读取、控制和主动感知权限将被撤销。", "清空白名单", { type: "warning" });
    ha.value.entities = [];
    ElMessage.success("白名单已清空");
  } catch { /* user cancelled */ }
}

function defaultActionsForDomain(domain: string): Action[] {
  if (domain === "light" || domain === "switch") return ["turn_on", "turn_off", "toggle"];
  if (domain === "climate") return ["turn_on", "turn_off", "set_temperature"];
  return [];
}

function autoAuthorize() {
  if (!ha.value) return;
  const ignoredDomains = new Set([
    "automation", "script", "scene",
    "input_boolean", "input_text", "input_number", "input_select", "input_datetime", "input_button",
    "timer", "counter", "zone", "person", "sun", "weather",
    "update", "persistent_notification", "group",
  ]);
  const existing = new Set(ha.value.entities.map((item) => item.entity_id));
  let added = 0;
  for (const item of allEntities.value) {
    if (existing.has(item.entity_id)) continue;
    if (ignoredDomains.has(item.domain)) continue;
    const actions = defaultActionsForDomain(item.domain);
    ha.value.entities.push({
      entity_id: item.entity_id,
      display_name: item.friendly_name,
      aliases: [],
      read_allowed: true,
      history_allowed: false,
      history_max_hours: 24,
      allowed_actions: actions,
      confirmation_required_actions: [],
      proactive_rules: [{
        rule_id: "device_offline",
        kind: "device_offline",
        enabled: true,
        threshold: null,
        duration_seconds: 120,
        cooldown_minutes: 240,
        message: null,
      }],
      privacy_level: "L1",
      allowed_attributes: ["friendly_name", "device_class", "unit_of_measurement"],
    });
    added++;
  }
  ElMessage.success(`已自动授权 ${added} 个实体`);
}

onMounted(load);
</script>

<template>
  <section v-loading="loading" class="ha-workspace">
    <div class="hero panel">
      <div><div class="eyebrow">设备与授权 · Home Assistant</div><h2>HA 实体授权</h2><p>统一管理实体发现、读取、历史、控制、确认和主动感知。未授权能力默认拒绝。</p></div>
      <div class="actions"><el-button :loading="fetchingAll" @click="() => fetchAllEntities()">刷新设备列表</el-button><el-button type="primary" :loading="saving" @click="save">保存并生效</el-button></div>
    </div>

    <template v-if="ha">
      <div v-if="ha.enabled" class="panel discovery">
        <div class="panel-head"><div><h2>HA 设备总览</h2><p>共 {{ allEntities.length }} 个实体 · 已授权 {{ ha.entities.length }} 个</p></div><el-button type="primary" :disabled="!selected.length" @click="addSelected">加入授权（{{ selected.length }}）</el-button></div>
        <div class="filter-bar">
          <el-input v-model="keyword" clearable placeholder="按名称、实体 ID、区域或类型搜索" style="width:260px" />
          <el-select v-model="areaFilter" clearable placeholder="全部区域" style="width:160px">
            <el-option v-for="area in areaOptions" :key="area" :label="area" :value="area" />
          </el-select>
          <el-select v-model="domainFilter" clearable placeholder="全部类型" style="width:160px">
            <el-option v-for="domain in domainOptions" :key="domain" :label="domain" :value="domain" />
          </el-select>
        </div>
        <el-table :data="filteredEntities" max-height="480" @selection-change="rows => selected = rows.map((row: EntityDetail) => row.entity_id)">
          <el-table-column type="selection" width="48" />
          <el-table-column label="名称" min-width="180">
            <template #default="{ row }">
              <span>{{ row.friendly_name }}</span>
              <small v-if="authorizedIds.has(row.entity_id)" class="auth-badge">已授权</small>
            </template>
          </el-table-column>
          <el-table-column prop="entity_id" label="实体 ID" min-width="240" />
          <el-table-column label="区域" width="130">
            <template #default="{ row }">
              <el-tag v-if="row.area" size="small" type="info" effect="plain">{{ row.area }}</el-tag>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="类型" width="110">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ row.domain }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="state" label="状态" width="100" />
        </el-table>
      </div>

      <div class="panel proactive-global">
        <div class="panel-head"><div><h2>主动感知总策略</h2><p>只主动发送建议和安全提醒，不会由 HA 事件自动控制设备。</p></div><div class="actions"><el-button :loading="testingProactive" @click="testProactive">发送测试提醒</el-button><el-switch v-model="ha.proactive_enabled" active-text="启用主动感知" /></div></div>
        <div class="form-grid">
          <label><span>免打扰开始</span><el-time-select v-model="ha.proactive_quiet_hours_start" start="00:00" step="00:30" end="23:30" /></label>
          <label><span>免打扰结束</span><el-time-select v-model="ha.proactive_quiet_hours_end" start="00:00" step="00:30" end="23:30" /></label>
          <label><span>每日上限</span><el-input-number v-model="ha.proactive_daily_limit" :min="1" :max="50" /><small>条</small></label>
          <label><span>安全事件</span><el-switch v-model="ha.proactive_critical_bypasses_quiet_hours" active-text="可越过免打扰" /></label>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head"><div><h2>实体权限白名单</h2><p>每个实体独立配置最小权限和主动规则。</p></div><div class="actions"><el-button size="small" type="danger" plain @click="clearWhitelist">清空白名单</el-button><el-button size="small" type="primary" @click="autoAuthorize">智能授权</el-button><el-tag effect="plain">{{ ha.entities.length }} 个实体</el-tag></div></div>
        <el-empty v-if="!ha.entities.length" description="请先同步 HA 并加入需要授权的实体" />
        <div v-for="(entity, index) in ha.entities" :key="entity.entity_id" class="entity-card">
          <div class="entity-head"><div><strong>{{ entity.display_name }}</strong><code>{{ entity.entity_id }}</code></div><div class="actions"><el-switch v-model="entity.read_allowed" active-text="允许读取" /><el-button size="small" type="danger" plain @click="removeEntity(index)">移除</el-button></div></div>
          <div class="form-grid three">
            <label><span>显示名称</span><el-input v-model="entity.display_name" /></label>
            <label><span>别名</span><el-select v-model="entity.aliases" multiple filterable allow-create default-first-option /></label>
            <label><span>隐私等级</span><el-select v-model="entity.privacy_level"><el-option v-for="level in ['L0','L1','L2','L3']" :key="level" :label="level" :value="level" /></el-select></label>
            <label><span>历史与 Logbook</span><el-switch v-model="entity.history_allowed" active-text="允许" /></label>
            <label><span>历史最大范围</span><el-input-number v-model="entity.history_max_hours" :min="1" :max="168" :disabled="!entity.history_allowed" /><small>小时</small></label>
            <label v-if="supportedActions(entity.entity_id).length"><span>允许控制</span><el-select v-model="entity.allowed_actions" multiple><el-option v-for="action in supportedActions(entity.entity_id)" :key="action" :label="actionLabels[action]" :value="action" /></el-select></label>
            <label v-if="entity.allowed_actions.length"><span>需要明确确认</span><el-select v-model="entity.confirmation_required_actions" multiple><el-option v-for="action in entity.allowed_actions" :key="action" :label="actionLabels[action]" :value="action" /></el-select></label>
            <label class="wide"><span>允许返回的属性</span><el-select v-model="entity.allowed_attributes" multiple filterable allow-create default-first-option /></label>
          </div>
          <div class="rules">
            <div class="rules-head"><div><strong>主动感知规则</strong><small>规则需同时满足持续时间、冷却、免打扰和每日上限。</small></div><el-button size="small" @click="addRule(entity)">添加规则</el-button></div>
            <div v-for="(rule, ruleIndex) in entity.proactive_rules" :key="rule.rule_id" class="rule-row">
              <el-switch v-model="rule.enabled" />
              <el-select v-model="rule.kind" @change="onRuleKindChange(rule)"><el-option v-for="kind in supportedRules(entity)" :key="kind" :label="ruleLabels[kind]" :value="kind" /></el-select>
              <label v-if="rule.threshold !== null"><span>阈值</span><el-input-number v-model="rule.threshold" :step="0.5" /></label>
              <label><span>持续</span><el-input-number v-model="rule.duration_seconds" :min="0" :max="86400" /><small>秒</small></label>
              <label><span>冷却</span><el-input-number v-model="rule.cooldown_minutes" :min="1" :max="10080" /><small>分钟</small></label>
              <el-input v-model="rule.message" clearable placeholder="自定义提醒文案（可选）" />
              <el-button type="danger" link @click="entity.proactive_rules.splice(ruleIndex, 1)">删除</el-button>
            </div>
          </div>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.ha-workspace{padding:20px 24px 28px;display:grid;gap:16px;align-content:start}.panel{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px}.hero,.panel-head,.entity-head,.rules-head,.actions{display:flex;align-items:center;justify-content:space-between;gap:14px}.hero{background:linear-gradient(135deg,#fff,#f1f5ff)}h2,p{margin:0}.hero h2,.panel h2{font-size:16px}.hero p,.panel-head p{margin-top:7px;color:var(--muted);font-size:12px}.eyebrow{margin-bottom:7px;color:var(--accent);font-size:11px;font-weight:700}.actions{justify-content:flex-end}.form-grid{display:grid;grid-template-columns:repeat(4,minmax(160px,1fr));gap:14px}.form-grid.three{grid-template-columns:repeat(3,minmax(180px,1fr))}.form-grid label,.rule-row label{display:grid;gap:6px;color:var(--muted);font-size:11px}.form-grid small,.rule-row small,.rules-head small{color:var(--muted);font-size:10px}.wide{grid-column:1/-1}.discovery{display:grid;gap:14px}.filter-bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.auth-badge{display:inline-block;margin-left:6px;padding:1px 6px;border-radius:4px;background:#e8f5e9;color:#2e7d32;font-size:10px;font-weight:600}.muted{color:var(--muted);font-size:12px}.entity-card{display:grid;gap:16px;margin-top:14px;padding:16px;border:1px solid #e5e9f2;border-radius:12px;background:#fbfcff}.entity-head>div:first-child{display:grid;gap:5px}.entity-head code{color:var(--muted);font-size:10px}.rules{display:grid;gap:10px;padding-top:14px;border-top:1px dashed #dfe4ee}.rules-head>div{display:grid;gap:4px}.rule-row{display:grid;grid-template-columns:auto minmax(150px,1fr) repeat(3,minmax(105px,auto)) minmax(200px,1.4fr) auto;gap:10px;align-items:end;padding:11px;border:1px solid #e7ebf3;border-radius:9px;background:#fff}@media(max-width:1100px){.form-grid,.form-grid.three{grid-template-columns:repeat(2,minmax(160px,1fr))}.rule-row{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:700px){.hero,.panel-head,.entity-head{align-items:flex-start;flex-direction:column}.form-grid,.form-grid.three,.rule-row{grid-template-columns:1fr}.wide{grid-column:auto}.filter-bar{flex-direction:column;align-items:stretch}}
</style>
