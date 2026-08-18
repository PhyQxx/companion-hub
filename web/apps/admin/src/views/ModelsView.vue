<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { AdminApi } from "@aria/shared";

interface HubModel {
  name?: string;
  provider: string;
  model: string;
  enabled: boolean;
  runs_local: boolean;
  max_privacy_level: string;
  base_url: string;
}
interface HubConfig {
  schema_version: number;
  models: Record<string, HubModel>;
  routes: Record<string, { primary: string; fallbacks?: string[] }>;
}
interface VersionRow {
  version: number;
  status: string;
  content_hash: string;
  created_by: string;
  created_at: string;
  rollback_from_version?: number | null;
}

// 模型与路由：当前配置摘要 + JSON 草稿编辑器 + 版本历史。
// 保存只产生草稿（先 /validate 再建草稿），发布/回滚均为原子操作。
const api = inject("adminApi") as AdminApi;
const emit = defineEmits<{ status: [text: string, error?: boolean] }>();

const current = ref<{ version: number; content_hash: string; published_at: string; config: HubConfig } | null>(null);
const versions = ref<VersionRow[]>([]);
const draftJson = ref("");
const busy = ref(false);

async function load() {
  try {
    const [config, history] = await Promise.all([
      api.request<typeof current.value>("/api/v1/admin/config/current"),
      api.request<VersionRow[]>("/api/v1/admin/config/versions"),
    ]);
    current.value = config;
    versions.value = history;
    draftJson.value = JSON.stringify(config?.config ?? {}, null, 2);
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "加载失败", true);
  }
}

async function saveDraft() {
  busy.value = true;
  try {
    const candidate = JSON.parse(draftJson.value) as HubConfig;
    await api.request("/api/v1/admin/config/validate", {
      method: "POST",
      body: JSON.stringify(candidate),
    });
    const draft = await api.request<VersionRow>("/api/v1/admin/config/versions", {
      method: "POST",
      body: JSON.stringify(candidate),
    });
    emit("status", `草稿 v${draft.version} 已保存，发布后生效`);
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "草稿无效", true);
  } finally {
    busy.value = false;
  }
}

async function publish(version: number) {
  if (!confirm(`发布配置草稿 v${version}？当前配置会被替换。`)) return;
  try {
    await api.request(`/api/v1/admin/config/versions/${version}/publish`, { method: "POST" });
    emit("status", `配置 v${version} 已发布`);
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "发布失败", true);
  }
}

async function rollback(version: number) {
  if (!confirm(`回滚到配置 v${version}？会创建一个新的已发布版本。`)) return;
  try {
    const result = await api.request<{ version: number }>(
      `/api/v1/admin/config/versions/${version}/rollback`,
      { method: "POST" },
    );
    emit("status", `已创建回滚版本 v${result.version}`);
    await load();
  } catch (error) {
    emit("status", error instanceof Error ? error.message : "回滚失败", true);
  }
}

onMounted(load);
</script>

<template>
  <section class="content" v-if="current">
    <div class="stats">
      <article><span>当前版本</span><strong>v{{ current.version }}</strong></article>
      <article><span>启用模型</span><strong>{{ Object.values(current.config.models).filter((m) => m.enabled).length }}</strong></article>
      <article><span>内容哈希</span><strong class="hash">{{ current.content_hash.slice(0, 12) }}</strong></article>
    </div>

    <div class="panel">
      <h2>模型与路由</h2>
      <table>
        <thead><tr><th>名称</th><th>模型</th><th>本地</th><th>隐私上限</th><th>启用</th></tr></thead>
        <tbody>
          <tr v-for="(model, name) in current.config.models" :key="name">
            <td><strong>{{ name }}</strong></td>
            <td>{{ model.model }}</td>
            <td>{{ model.runs_local ? "本地" : "云端" }}</td>
            <td>{{ model.max_privacy_level }}</td>
            <td>{{ model.enabled ? "✓" : "—" }}</td>
          </tr>
        </tbody>
      </table>
      <p class="routes">
        路由：<code v-for="(policy, route) in current.config.routes" :key="route">
          {{ route }} → {{ policy.primary }}{{ policy.fallbacks?.length ? ` (+${policy.fallbacks.join(",")})` : "" }}
        </code>
      </p>
    </div>

    <div class="panel">
      <h2>草稿编辑（JSON）</h2>
      <p class="hint">保存只产生草稿；校验失败不会落库。密钥仍只允许 env: 引用。</p>
      <textarea v-model="draftJson" rows="14" spellcheck="false" />
      <div class="row">
        <button class="primary" :disabled="busy" @click="saveDraft">校验并保存草稿</button>
      </div>
    </div>

    <div class="panel">
      <h2>版本历史</h2>
      <table>
        <thead><tr><th>版本</th><th>状态</th><th>创建人</th><th>时间</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="item in versions" :key="item.version">
            <td><strong>v{{ item.version }}</strong><small v-if="item.rollback_from_version"> ↩ v{{ item.rollback_from_version }}</small></td>
            <td><span class="status" :class="item.status">{{ item.status }}</span></td>
            <td>{{ item.created_by }}</td>
            <td><small>{{ new Date(item.created_at).toLocaleString() }}</small></td>
            <td>
              <button v-if="item.status === 'draft'" class="primary" @click="publish(item.version)">发布</button>
              <button v-else @click="rollback(item.version)">回滚到此版</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.content { padding: 20px 24px; display: grid; gap: 16px; align-content: start; overflow-y: auto; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
.stats article { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 12px; display: grid; gap: 4px; }
.stats span { color: var(--muted); font-size: 12px; }
.stats strong { font-size: 18px; }
.hash { font-size: 13px; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px; display: grid; gap: 10px; }
.panel h2 { margin: 0; font-size: 15px; }
.hint { color: var(--muted); font-size: 12px; margin: 0; }
textarea { width: 100%; resize: vertical; font-family: ui-monospace, monospace; font-size: 12px; line-height: 1.5; }
.row { display: flex; gap: 10px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; padding: 8px; border-bottom: 1px solid var(--line); }
td { padding: 8px; border-bottom: 1px solid #1c1f2b; vertical-align: top; }
small { color: var(--muted); display: block; font-size: 11px; }
.routes code { display: inline-block; margin: 0 10px 4px 0; color: #c9cfe6; font-size: 12px; }
.status { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 11px; border: 1px solid; }
.status.published { color: #7ee2a8; border-color: #2c5c41; background: #12241b; }
.status.draft { color: #f2c078; border-color: #6b5325; background: #2a2113; }
.status.superseded { color: #b7a6f0; border-color: #4b3f76; background: #1d1830; }
</style>
