import { getServiceUrl } from '../api';
import RequestService from '../httpRequest';

/**
 * 本地语音服务探测接口（本项目自有文件）
 *
 * 契约（后端实现）：
 *   POST {getServiceUrl()}/models/probe
 *   body   { "probe": "ovs_tts_speakers", "endpoint": "192.168.1.50:8621", "apiKey": "" }
 *   200 OK Result 包装，data = { "defaultSpeakerId": 0, "speakers": [{id,label,type}, ...] }
 *   失败   Result.code 为上游状态码，msg 是真实原因。
 *
 * 关于 401：上游的 401/403 由后端 common/utils/UpstreamStatus.java 重映射成
 * 4010/4030（msg 保留真实原因），因此 Result.code 不可能再出现 401 ——
 * 401 在本代码库里重新回到它的保留语义：仅表示对 manager-api 未认证。
 * 这样就能安全地走统一的 RequestService，不必自建 HTTP 客户端。
 * 其余码（400 / 404 / 503 …）原样透传。
 *
 * 这里同时挂 fail 和 networkFail 接管两条错误分支：httpRequest.js 的
 * httpHandlerError 只在回调缺席时才 showDanger 弹全局 toast，而探测失败必须内联
 * 显示在下拉框旁边，不能打断正在填表单的用户。也刻意不调用
 * RequestService.reAjaxFun —— 那会弹「正在连接服务器」并自动重试，
 * 对一个用户主动点击的探测动作来说是错的。
 */
export default {
  /**
   * @param {object} params       { probe, endpoint, apiKey }
   * @param {function} onSuccess  (data) => void   Result.data
   * @param {function} onError    (message, code) => void   文案一律取后端 msg
   */
  probe({ probe, endpoint, apiKey }, onSuccess, onError) {
    const fail = (message, code) => {
      RequestService.clearRequestTime();
      if (onError) onError(message, code);
    };

    RequestService.sendRequest()
      .url(`${getServiceUrl()}/models/probe`)
      .method('POST')
      .data({ probe, endpoint, apiKey: apiKey || '' })
      .success((res) => {
        RequestService.clearRequestTime();
        const body = (res && res.data) || {};
        if (onSuccess) onSuccess(body.data);
      })
      .fail((res) => {
        // 后端返回了业务错误码（4010 / 4030 / 400 / 404 / 503 …）
        const body = (res && res.data) || {};
        fail(body.msg || `探测失败（${body.code}）`, body.code);
      })
      .networkFail((res) => {
        // HTTP 层就失败了（探测端点未部署、网关不可达等）
        const status = (res && res.status) || 0;
        const body = (res && res.data) || {};
        fail(body.msg || `网络请求出现了错误【${status}】`, status);
      })
      .send();
  },
};
