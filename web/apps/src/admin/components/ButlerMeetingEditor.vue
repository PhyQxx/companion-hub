<script setup lang="ts">
import { inject, ref } from 'vue';
import { AdminApi } from '@aria/shared';
const props = defineProps<{ meeting: { id: string; status: string; participants: string[]; transcript_segments?: Array<{ speaker: string; text: string }>; action_items: Array<{ title: string; status: string }> } }>();
const emit = defineEmits<{ updated: [] }>();
const api = inject('adminApi') as AdminApi;
const text = ref(''); const speaker = ref('我'); const busy = ref(false); const error = ref('');
const dates = ref<Record<number, string>>({});
async function call(path: string, body: unknown = {}) {
  busy.value = true; error.value = '';
  try {
    await api.request(`/api/v1/admin/butler/meetings/${props.meeting.id}/${path}`, { method: 'POST', body: JSON.stringify(body) });
    if (path === 'transcript') text.value = '';
    emit('updated');
  } catch (e) { error.value = e instanceof Error ? e.message : String(e); }
  finally { busy.value = false; }
}
</script>
<template>
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <p v-for="(segment, index) in meeting.transcript_segments ?? []" :key="index">{{ segment.speaker }}：{{ segment.text }}</p>
  <el-button v-if="meeting.status === 'prepared' || meeting.status === 'consent_revoked'" :disabled="busy" @click="call('transcription/authorize', { authorized: true })">我已获得参与人同意，授权保存会议文本</el-button>
  <div v-if="meeting.status === 'recording'">
    <el-button :disabled="busy" @click="call('transcription/revoke')">撤销文本保存授权</el-button>
    <el-select v-model="speaker" aria-label="发言人"><el-option label="我" value="我" /><el-option v-for="person in meeting.participants.filter(p => p !== '我')" :key="person" :label="person" :value="person" /></el-select>
    <el-input v-model="text" type="textarea" :rows="5" :maxlength="4000" placeholder="输入会议笔记或粘贴已获授权的转写文本" />
    <el-button :disabled="busy || !text.trim()" @click="call('transcript', { segments: [{ speaker, text: text.trim() }] })">保存文本</el-button>
    <el-button :disabled="busy || !meeting.transcript_segments?.length || !!text.trim()" @click="call('finish')">结束并生成摘要</el-button>
  </div>
  <div v-for="(item, index) in meeting.action_items" :key="index">
    <template v-if="item.status !== 'created' && meeting.status === 'completed'">
      <p>{{ item.title }}</p><el-date-picker v-model="dates[index]" type="datetime" value-format="YYYY-MM-DDTHH:mm:ssZ" placeholder="选择任务截止时间" />
      <el-button :disabled="busy || !dates[index]" @click="call(`actions/${index}/confirm`, { due_at: dates[index] })">确认创建任务</el-button>
    </template>
  </div>
</template>
