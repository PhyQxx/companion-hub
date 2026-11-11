<script setup lang="ts">
import { inject, ref, watch } from "vue";
import { AdminApi } from "@aria/shared";
const props = defineProps<{ modelValue: boolean; kind: 'workflows' | 'scenes' | 'meetings' }>();
const emit = defineEmits<{ 'update:modelValue': [boolean]; created: [] }>();
const api = inject('adminApi') as AdminApi;
interface Action { action_id: string; label: string; arguments_schema: { properties?: Record<string, { type?: string; title?: string; description?: string }> } }
interface Step { action_id: string; arguments: Record<string, unknown> }
interface Preview { steps: Array<Step & { label: string; risk: string; confirmation_policy: string; reversible: boolean }> }
const name = ref('');
const trigger = ref('manual');
const participants = ref('');
const actions = ref<Action[]>([]);
const steps = ref<Step[]>([]);
const preview = ref<Preview | null>(null);
const error = ref('');
const busy = ref(false);
const labels = { workflows: '流程', scenes: '场景', meetings: '会议' };
watch(() => props.modelValue, async open => {
  if (!open) return;
  name.value = ''; trigger.value = 'manual'; participants.value = ''; steps.value = []; preview.value = null; error.value = '';
  if (props.kind === 'meetings') return;
  busy.value = true;
  try { actions.value = await api.request<Action[]>('/api/v1/admin/butler/actions'); }
  catch (e) { error.value = String(e); }
  finally { busy.value = false; }
});
function fields(step: Step) { return actions.value.find(a => a.action_id === step.action_id)?.arguments_schema.properties ?? {}; }
function argument(step: Step, key: string, value: string, type?: string) {
  step.arguments[key] = type === 'integer' || type === 'number' ? Number(value) : type === 'boolean' ? value === 'true' : value;
}
async function submit() {
  error.value = ''; busy.value = true;
  try {
    if (!name.value.trim()) throw new Error('请填写名称');
    const body = { name: name.value.trim(), steps: steps.value };
    if (props.kind !== 'meetings' && !preview.value) {
      preview.value = await api.request<Preview>('/api/v1/admin/butler/workflows/preview', { method: 'POST', body: JSON.stringify(body) });
      return;
    }
    const payload = props.kind === 'meetings' ? { title: name.value.trim(), participants: participants.value.split(/[，,、\n]/).map(x => x.trim()).filter(Boolean), privacy_level: 'L1' }
      : props.kind === 'scenes' ? { ...body, trigger: trigger.value } : body;
    await api.request(`/api/v1/admin/butler/${props.kind}`, { method: 'POST', body: JSON.stringify(payload) });
    emit('update:modelValue', false); emit('created');
  } catch (e) { error.value = e instanceof Error ? e.message : String(e); }
  finally { busy.value = false; }
}
</script>
<template>
  <el-dialog :model-value="modelValue" :title="`新建${labels[kind]}`" width="min(720px, 95vw)" :close-on-click-modal="false" :before-close="(done: () => void) => { if (!busy) { emit('update:modelValue', false); done(); } }">
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-form label-position="top" :disabled="busy || preview !== null">
      <el-form-item label="名称"><el-input v-model="name" :maxlength="120" /></el-form-item>
      <el-form-item v-if="kind === 'meetings'" label="参与人（用逗号分隔）"><el-input v-model="participants" /></el-form-item>
      <el-form-item v-if="kind === 'scenes'" label="触发条件">
        <el-select v-model="trigger"><el-option label="手动" value="manual" /><el-option label="到家" value="user_arrived_home" /><el-option label="离家" value="user_left_home" /></el-select>
      </el-form-item>
      <template v-if="kind !== 'meetings'">
        <div v-for="(step, index) in steps" :key="index">
          <el-form-item :label="`动作 ${index + 1}`"><el-select v-model="step.action_id" filterable @change="step.arguments = {}"><el-option v-for="action in actions" :key="action.action_id" :label="action.label" :value="action.action_id" /></el-select><el-button @click="steps.splice(index, 1)">删除</el-button></el-form-item>
          <el-form-item v-for="(field, key) in fields(step)" :key="key" :label="field.description ?? field.title ?? key"><el-input :model-value="String(step.arguments[key] ?? '')" @update:model-value="value => argument(step, key, value, field.type)" /></el-form-item>
        </div>
        <el-button :disabled="steps.length >= 10" @click="steps.push({ action_id: '', arguments: {} })">添加动作（最多 10 步）</el-button>
      </template>
    </el-form>
    <div v-if="preview">
      <h3>确认保存以下动作</h3>
      <p>保存后不会立即执行动作。场景保存后启用，触发时生成待确认计划。</p>
      <div v-for="(step, index) in preview.steps" :key="index"><strong>{{ index + 1 }}. {{ step.label }}</strong><p>风险 {{ step.risk }} · 确认策略 {{ step.confirmation_policy }} · {{ step.reversible ? '可撤销' : '不可撤销' }}</p><pre>{{ JSON.stringify(step.arguments, null, 2) }}</pre></div>
    </div>
    <template #footer><el-button v-if="preview" :disabled="busy" @click="preview = null">返回修改</el-button><el-button :disabled="busy" @click="emit('update:modelValue', false)">取消</el-button><el-button type="primary" :loading="busy" @click="submit">{{ kind === 'meetings' ? '创建会议' : preview ? '确认保存' : '预览动作与权限' }}</el-button></template>
  </el-dialog>
</template>
