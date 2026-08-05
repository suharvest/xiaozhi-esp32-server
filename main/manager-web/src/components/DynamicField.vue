<template>
  <div class="dynamic-field">
    <!-- JSON 文本域（由 type: dict 或 json-textarea 映射而来） -->
    <template v-if="widget === 'json-textarea'">
      <el-input
        :value="jsonValue"
        type="textarea"
        :rows="3"
        :class="inputClass"
        :placeholder="jsonPlaceholder"
        @input="(val) => $emit('json-input', val)"
        @change="(val) => $emit('json-change', val)"
        @focus="$emit('focus')"
        @blur="$emit('blur')"
      ></el-input>
    </template>

    <!-- 数字 -->
    <template v-else-if="widget === 'number'">
      <el-input-number
        :value="numberValue"
        :min="numberMin"
        :max="numberMax"
        :step="numberStep"
        :precision="field.precision"
        controls-position="right"
        style="width: 100%"
        :placeholder="placeholder"
        @input="(val) => $emit('input', val)"
        @focus="$emit('focus')"
        @blur="$emit('blur')"
      ></el-input-number>
    </template>

    <!-- 开关 -->
    <template v-else-if="widget === 'boolean'">
      <el-switch
        :value="booleanValue"
        @change="(val) => $emit('input', val)"
      ></el-switch>
    </template>

    <!-- 静态下拉 -->
    <template v-else-if="widget === 'select'">
      <el-select
        :value="value"
        :class="inputClass"
        style="width: 100%"
        :placeholder="placeholder"
        :clearable="!field.required"
        filterable
        @change="(val) => $emit('input', val)"
        @focus="$emit('focus')"
        @blur="$emit('blur')"
      >
        <el-option
          v-for="opt in staticOptions"
          :key="String(opt.value)"
          :label="opt.label"
          :value="opt.value"
        ></el-option>
      </el-select>
    </template>

    <!-- 远程下拉：选项由父组件 probe 拉回来后通过 remoteOptions 传入 -->
    <template v-else-if="widget === 'remote-select'">
      <div class="remote-select">
        <el-select
          :value="value"
          :class="inputClass"
          class="remote-select__input"
          :placeholder="remotePlaceholder"
          :clearable="!field.required"
          :loading="loading"
          filterable
          @change="(val) => $emit('input', val)"
          @focus="$emit('focus')"
          @blur="$emit('blur')"
          @visible-change="handleVisibleChange"
        >
          <el-option
            v-for="opt in mergedRemoteOptions"
            :key="String(opt.value)"
            :label="opt.label"
            :value="opt.value"
          ></el-option>
        </el-select>
        <el-button
          class="remote-select__refresh"
          icon="el-icon-refresh"
          :loading="loading"
          @click="$emit('probe', field.optionsFrom)"
        ></el-button>
      </div>
      <!-- 失败时内联提示，不弹全局 toast -->
      <div v-if="errorMessage" class="remote-select__error">{{ errorMessage }}</div>
      <div v-else-if="remoteHint" class="remote-select__hint">{{ remoteHint }}</div>
    </template>

    <!-- URL -->
    <template v-else-if="widget === 'url'">
      <el-input
        :value="value"
        type="text"
        :class="inputClass"
        :placeholder="placeholder"
        @input="(val) => $emit('input', val)"
        @focus="$emit('focus')"
        @blur="$emit('blur')"
      ></el-input>
      <div v-if="urlWarning" class="field-warning">{{ urlWarning }}</div>
    </template>

    <!-- text / password（默认，未知 type 也退化到这里） -->
    <el-input
      v-else
      :value="value"
      :type="widget === 'password' ? 'password' : 'text'"
      :show-password="widget === 'password'"
      :class="inputClass"
      :placeholder="placeholder"
      @input="(val) => $emit('input', val)"
      @focus="$emit('focus')"
      @blur="$emit('blur')"
    ></el-input>
  </div>
</template>

<script>
import { resolveWidget, normalizeRemoteOptions } from '@/utils/dynamicField';

/**
 * 模型配置表单的动态字段渲染器（本项目自有文件，永不与上游冲突）。
 *
 * 组件本身**无状态**：值、JSON 字符串、远程选项、loading、错误信息全部由父组件持有，
 * 敏感字段掩码逻辑（isSensitiveField / handleInputFocus / handleInputBlur /
 * handleJsonInputFocus / handleJsonInputBlur）继续留在父组件里，这里只把
 * focus / blur 原样透传出去，行为与抽组件之前完全一致。
 *
 * 控件派发规则见 @/utils/dynamicField.js 顶部注释（关键点：number / boolean
 * 是存量已有的 type 名，不能直接派发，必须走 field.widget 显式指定）。
 */
export default {
  name: 'DynamicField',
  props: {
    // 字段定义：{ prop/key, label, type, widget, options, optionsFrom, showWhen, default, placeholder, required }
    field: { type: Object, required: true },
    // 当前值（非 json-textarea 控件）
    value: { type: [String, Number, Boolean, Object, Array], default: '' },
    // json-textarea 的字符串形态（父组件的 fieldJsonMap[prop]）
    jsonValue: { type: String, default: '' },
    // 整个配置对象，供 showWhen / dependsOn 判断
    configJson: { type: Object, default: () => ({}) },
    // remote-select 的选项，由父组件 probe 后传入
    remoteOptions: { type: Array, default: () => [] },
    loading: { type: Boolean, default: false },
    // probe 失败原因，内联显示
    errorMessage: { type: String, default: '' },
    // 供 AddModelDialog 传 custom-input-bg，保证外观逐像素一致
    inputClass: { type: String, default: '' },
  },
  computed: {
    widget() {
      return resolveWidget(this.field);
    },
    placeholder() {
      return this.field.placeholder || '';
    },
    jsonPlaceholder() {
      return this.field.placeholder || this.$t('modelConfigDialog.enterJsonExample');
    },
    staticOptions() {
      return normalizeRemoteOptions(this.field.options || [], this.field.optionsFrom);
    },
    // 已选值不在拉回的选项里时（例如已保存的音色 id），补一条占位项，避免下拉显示空白
    mergedRemoteOptions() {
      const opts = (this.remoteOptions || []).slice();
      const v = this.value;
      if (v !== '' && v !== null && v !== undefined) {
        const hit = opts.some((o) => String(o.value) === String(v));
        if (!hit) opts.unshift({ label: String(v), value: v });
      }
      return opts;
    },
    remotePlaceholder() {
      return this.field.placeholder || '点击右侧按钮从设备拉取';
    },
    remoteHint() {
      if (this.loading) return '';
      if (this.remoteOptions && this.remoteOptions.length) return '';
      const dep = this.field.optionsFrom && this.field.optionsFrom.dependsOn;
      return dep ? `填好「${dep}」后展开即可自动拉取` : '';
    },
    numberValue() {
      if (this.value === '' || this.value === null || this.value === undefined) return undefined;
      const n = Number(this.value);
      return Number.isNaN(n) ? undefined : n;
    },
    numberMin() {
      return this.field.min === undefined ? -Infinity : this.field.min;
    },
    numberMax() {
      return this.field.max === undefined ? Infinity : this.field.max;
    },
    numberStep() {
      return this.field.step === undefined ? 1 : this.field.step;
    },
    booleanValue() {
      if (typeof this.value === 'boolean') return this.value;
      return this.value === 'true' || this.value === 1 || this.value === '1';
    },
    urlWarning() {
      const v = this.value;
      if (!v || typeof v !== 'string') return '';
      if (/^(https?|wss?):\/\/.+/i.test(v.trim())) return '';
      return '格式示例：http://192.168.1.50:8621';
    },
  },
  methods: {
    /**
     * 展开下拉时若还没有选项，自动拉一次。
     *
     * 不这么做的话，用户点开下拉看到「无数据」，第一反应是功能坏了 —— 尽管
     * 旁边有「填好地址后点右侧按钮拉取」的提示，但没人会先读提示再点。
     * 实测走查时我自己也是先点下拉、看到空、才回头找刷新按钮的。
     *
     * 只在「选项为空 + 依赖字段已填 + 当前没在加载」时触发：
     * 已有选项就别重复打设备；依赖字段没填时拉了也必然失败，徒增一次报错。
     * 想强制刷新（比如换了设备地址）仍可点右侧按钮。
     */
    handleVisibleChange(visible) {
      if (!visible) return;
      if (this.loading) return;
      if (this.remoteOptions && this.remoteOptions.length) return;
      const from = this.field.optionsFrom;
      if (!from) return;
      const dep = from.dependsOn;
      if (dep && !(this.configJson || {})[dep]) return;
      this.$emit('probe', from);
    },
  },
};
</script>

<style lang="scss" scoped>
.dynamic-field {
  width: 100%;
}

.remote-select {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
}

.remote-select__input {
  flex: 1;
  min-width: 0;
}

.remote-select__refresh {
  flex: none;
  padding: 8px 10px;
}

.remote-select__error {
  margin-top: 2px;
  font-size: 12px;
  line-height: 16px;
  color: #f56c6c;
  word-break: break-all;
}

.remote-select__hint,
.field-warning {
  margin-top: 2px;
  font-size: 12px;
  line-height: 16px;
  color: #909399;
}
</style>
