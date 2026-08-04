import Api from '@/apis/api';
import { toHostPort, normalizeRemoteOptions } from '@/utils/dynamicField';

/**
 * remote-select 的拉取逻辑（本项目自有文件）。
 * 混入到 ModelEditDialog / AddModelDialog 后，模板里只需要绑三个 map，
 * 上游文件的净足迹保持在几行以内。
 *
 * 失败一律写进 remoteErrorMap，由 DynamicField 内联展示在下拉框下方，
 * 不弹全局 toast（用户正在填表单，弹窗打断很烦）。
 */
export default {
  data() {
    return {
      remoteOptionsMap: {},
      remoteLoadingMap: {},
      remoteErrorMap: {},
    };
  },
  methods: {
    resetRemoteOptions() {
      this.remoteOptionsMap = {};
      this.remoteLoadingMap = {};
      this.remoteErrorMap = {};
    },
    /**
     * @param {object} field       dynamicCallInfoFields 里的字段
     * @param {object} configJson  当前整份配置
     */
    handleFieldProbe(field, configJson) {
      const prop = field.prop || field.key;
      const from = field.optionsFrom || {};
      const config = configJson || {};

      if (!from.probe) {
        this.$set(this.remoteErrorMap, prop, '该字段未配置 optionsFrom.probe');
        return;
      }

      const dep = from.dependsOn;
      const rawEndpoint = dep ? config[dep] : '';
      const endpoint = toHostPort(rawEndpoint);
      if (!endpoint) {
        this.$set(this.remoteErrorMap, prop, `请先填写「${dep || '服务地址'}」`);
        return;
      }

      this.$set(this.remoteErrorMap, prop, '');
      this.$set(this.remoteLoadingMap, prop, true);

      Api.probe.probe(
        { probe: from.probe, endpoint, apiKey: config.api_key || '' },
        (data) => {
          this.$set(this.remoteLoadingMap, prop, false);
          const options = normalizeRemoteOptions(data, from);
          this.$set(this.remoteOptionsMap, prop, options);
          if (!options.length) {
            this.$set(this.remoteErrorMap, prop, '设备返回了空列表');
            return;
          }
          // 换 TTS 模型后音色 id 完全不可迁移：当前值不在新列表里就回落到设备给的默认项
          const current = config[prop];
          const hit = options.some((o) => String(o.value) === String(current));
          if (hit) return;
          // defaultKey 没显式声明时，认 data 里第一个 default* 字段（契约里是 defaultSpeakerId）
          let defaultKey = from.defaultKey;
          if (!defaultKey && data && typeof data === 'object') {
            defaultKey = Object.keys(data).find((k) => /^default/i.test(k));
          }
          const fallback =
            defaultKey && data && data[defaultKey] !== undefined
              ? data[defaultKey]
              : options[0].value;
          this.$set(config, prop, fallback);
        },
        (message, code) => {
          this.$set(this.remoteLoadingMap, prop, false);
          this.$set(this.remoteErrorMap, prop, code ? `${message}` : message);
        }
      );
    },
  },
};
