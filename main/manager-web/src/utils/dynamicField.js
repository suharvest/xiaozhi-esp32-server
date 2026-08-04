/**
 * DynamicField 的 schema 解析工具（本项目自有文件，不来自上游）
 *
 * 背景 / 为什么不能直接拿 field.type 当控件名
 * ------------------------------------------------
 * 上游 ai_model_provider.fields 里已经存在大量 type 声明：
 *   string(310) / number(83) / dict(22) / boolean(18) / password(6) / float / int / integer / array
 * 而现在的渲染层（ModelEditDialog.vue 旧 308-318 行）只认 dict → JSON 文本域、
 * password → 密码框，其余一律塌缩成普通文本框。
 *
 * 也就是说：存量的 number / boolean 字段今天**渲染成文本框**。
 * 如果新渲染器直接把 type === 'number' 映射到 el-input-number、
 * type === 'boolean' 映射到 el-switch，那 101 个存量字段的表单外观会立刻变样，
 * 违反「任何存量 provider 的表单外观都不能变」这条硬要求。
 *
 * 因此采用「不冲突的新名字 + 显式 widget 逃生舱」两条路：
 *   1) field.widget = 'number' | 'boolean' | ...   ← 显式指定，优先级最高
 *   2) field.type ∈ 新增且与存量不冲突的名字：
 *      text / password / json-textarea / url / select / remote-select
 * 其余（string/number/boolean/float/int/integer/array/未知）一律退化成 text，
 * dict 依旧映射到 json-textarea —— 与改动前逐字节一致。
 */

// 渲染器支持的全部控件
export const WIDGETS = [
  'text',
  'password',
  'json-textarea',
  'number',
  'boolean',
  'url',
  'select',
  'remote-select',
];

// 可以直接由 field.type 派发的类型名 → 控件。
// 这些名字在存量数据里都不存在（唯一的例外 password 本来就已经渲染成密码框，行为不变），
// 所以直接派发不会改变任何存量 provider 的外观。
// 注意 number-input / switch 这两个别名：数字/开关控件不能挂在 type: 'number' /
// 'boolean' 上（那是存量已占用的名字），于是给它们起了不冲突的新名字。
export const TYPE_TO_WIDGET = {
  'text': 'text',
  'password': 'password',
  'json-textarea': 'json-textarea',
  'url': 'url',
  'select': 'select',
  'remote-select': 'remote-select',
  'number-input': 'number',
  'switch': 'boolean',
};

export const TYPE_DISPATCHABLE = Object.keys(TYPE_TO_WIDGET);

// 供 ProviderDialog 的「字段类型」下拉使用（不走 i18n 文件，避免改 6 份上游语言包）
export const WIDGET_TYPE_OPTIONS = [
  { value: 'text', zh: '文本(text)', en: 'Text (text)' },
  { value: 'password', zh: '密码(password)', en: 'Password (password)' },
  { value: 'url', zh: '地址(url)', en: 'URL (url)' },
  { value: 'json-textarea', zh: 'JSON 文本域(json-textarea)', en: 'JSON textarea (json-textarea)' },
  { value: 'number-input', zh: '数字输入框(number-input)', en: 'Number input (number-input)' },
  { value: 'switch', zh: '开关(switch)', en: 'Switch (switch)' },
  { value: 'select', zh: '下拉选择(select)', en: 'Select (select)' },
  { value: 'remote-select', zh: '远程下拉(remote-select)', en: 'Remote select (remote-select)' },
];

export function widgetTypeOptions(locale) {
  const zh = String(locale || '').indexOf('zh') === 0;
  return WIDGET_TYPE_OPTIONS.map((o) => ({ value: o.value, label: zh ? o.zh : o.en }));
}

export function widgetTypeLabel(type, locale) {
  const hit = WIDGET_TYPE_OPTIONS.find((o) => o.value === type);
  if (!hit) return undefined;
  return String(locale || '').indexOf('zh') === 0 ? hit.zh : hit.en;
}

/**
 * 解析字段应该用哪个控件。
 * @param {object} field
 * @param {object} [opts]
 * @param {string} [opts.dictFallback='json-textarea']
 *        type === 'dict' 时退化到哪个控件。ModelEditDialog 用默认值
 *        （它本来就把 dict 渲染成 JSON 文本域）；AddModelDialog 必须传 'text'，
 *        因为它改动前把 dict 也渲染成普通文本框，外观不能变。
 * @returns {string} WIDGETS 中的一个
 */
export function resolveWidget(field, opts) {
  const dictFallback = (opts && opts.dictFallback) || 'json-textarea';
  if (!field) return 'text';
  if (field.widget && WIDGETS.indexOf(field.widget) !== -1) {
    return field.widget;
  }
  if (TYPE_TO_WIDGET[field.type]) {
    return TYPE_TO_WIDGET[field.type];
  }
  if (field.type === 'dict') {
    return dictFallback;
  }
  return 'text';
}

/**
 * 该字段是否「主动接入了新 schema」。
 * 只有接入新 schema 的字段才会应用 default / required 等新语义，
 * 存量字段保持原样（例如 default 声明了也不预填，与改动前一致）。
 */
export function isEnhancedField(field) {
  if (!field) return false;
  if (field.widget) return true;
  if (field.options || field.optionsFrom || field.showWhen) return true;
  // password 排除：它是存量已有的 type
  return ['text', 'json-textarea', 'url', 'select', 'remote-select', 'number-input', 'switch']
    .indexOf(field.type) !== -1;
}

/**
 * showWhen 联动显示判断。没声明 showWhen 一律可见。
 * showWhen: { field: 'type', equals: 'openvoicestream_tts' }
 * 也支持 { field: 'type', in: ['a', 'b'] }
 */
export function isFieldVisible(field, configJson) {
  const rule = field && field.showWhen;
  if (!rule || !rule.field) return true;
  const current = configJson ? configJson[rule.field] : undefined;
  if (Array.isArray(rule.in)) {
    return rule.in.map(String).indexOf(String(current)) !== -1;
  }
  if (Object.prototype.hasOwnProperty.call(rule, 'equals')) {
    return String(current) === String(rule.equals);
  }
  return true;
}

/**
 * 字段的初始值。存量字段永远返回 ''（与改动前一致）。
 */
export function fieldDefault(field) {
  if (isEnhancedField(field) && field.default !== undefined && field.default !== null) {
    return field.default;
  }
  const widget = resolveWidget(field);
  if (widget === 'boolean') return false;
  if (widget === 'number') return undefined;
  return '';
}

/**
 * 把用户填的地址规整成后端 /models/probe 要求的 host:port。
 * 后端拒收 scheme / path / query / userinfo，前端负责剥掉。
 *   http://192.168.1.50:8621/v1  → 192.168.1.50:8621
 *   ws://user:pw@[::1]:8621/asr  → [::1]:8621
 */
export function toHostPort(raw) {
  if (raw === undefined || raw === null) return '';
  let s = String(raw).trim();
  if (!s) return '';
  // scheme://
  s = s.replace(/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//, '');
  // userinfo@
  const at = s.lastIndexOf('@');
  if (at !== -1) s = s.slice(at + 1);
  // path / query / fragment
  s = s.split('/')[0].split('?')[0].split('#')[0];
  return s.trim();
}

/**
 * 把 probe 返回的 data 规整成 [{label, value}]。
 * 契约示例：data = { defaultSpeakerId: 0, speakers: [{id, label, type}] }
 * @param {object|array} data     后端 Result.data
 * @param {object} optionsFrom    字段上的 optionsFrom
 */
export function normalizeRemoteOptions(data, optionsFrom) {
  const from = optionsFrom || {};
  const labelKey = from.labelKey || 'label';
  const valueKey = from.valueKey || 'value';

  let list = null;
  if (Array.isArray(data)) {
    list = data;
  } else if (data && typeof data === 'object') {
    if (from.listKey && Array.isArray(data[from.listKey])) {
      list = data[from.listKey];
    } else {
      const arrayKey = Object.keys(data).find((k) => Array.isArray(data[k]));
      list = arrayKey ? data[arrayKey] : [];
    }
  }
  if (!Array.isArray(list)) list = [];

  return list.map((item) => {
    if (item === null || typeof item !== 'object') {
      return { label: String(item), value: item };
    }
    const value = item[valueKey] !== undefined ? item[valueKey] : item.value;
    const label = item[labelKey] !== undefined ? item[labelKey] : String(value);
    return { label: String(label), value, raw: item };
  });
}

export default {
  WIDGETS,
  TYPE_DISPATCHABLE,
  TYPE_TO_WIDGET,
  WIDGET_TYPE_OPTIONS,
  widgetTypeOptions,
  widgetTypeLabel,
  resolveWidget,
  isEnhancedField,
  isFieldVisible,
  fieldDefault,
  toHostPort,
  normalizeRemoteOptions,
};
