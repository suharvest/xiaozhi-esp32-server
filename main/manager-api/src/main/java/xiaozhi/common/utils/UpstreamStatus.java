package xiaozhi.common.utils;

/**
 * 代理类控制器回显上游状态码时的重映射。
 *
 * <p>
 * <b>为什么需要这个：</b>前端 {@code httpRequest.js:122-125} 把
 * {@code info.data.code === 401} 无条件当作「manager-api 登录态失效」处理 ——
 * 直接 {@code clearAuth()} + 跳登录页，且发生在 {@code failCallback} 之前，
 * 业务代码无法拦截。
 * </p>
 *
 * <p>
 * 所以 {@code Result.code == 401} 在本代码库里是<b>保留语义</b>：只能表示
 * 「调用方对 manager-api 未认证」。任何把请求转发给外部服务的控制器
 * （仓管系统人脸接口、OVS/EdgeLLM 探测……）若原样透传上游的 401，
 * 后果是「外部服务的 key 填错 → 管理员被踢下线」—— 一个完全无关的副作用，
 * 现场极难定位。403 同理，避免与 shiro 的无权限语义混淆。
 * </p>
 *
 * <p>
 * 因此代理层统一走本方法重映射，把上游状态码挪出保留区间，
 * 前端仍能从 {@code Result.msg} 看到真实原因。
 * </p>
 */
public final class UpstreamStatus {

    /** 上游返回 401（凭证缺失/错误），非本站登录态问题。 */
    public static final int UPSTREAM_UNAUTHORIZED = 4010;

    /** 上游返回 403（无权限访问该资源）。 */
    public static final int UPSTREAM_FORBIDDEN = 4030;

    private UpstreamStatus() {
    }

    /**
     * 把上游 HTTP 状态码映射成可以安全放进 {@code Result.code} 的值。
     *
     * @param status 上游返回的 HTTP 状态码
     * @return 重映射后的业务码；不在保留区间的原样返回
     */
    public static int remap(int status) {
        if (status == 401) {
            return UPSTREAM_UNAUTHORIZED;
        }
        if (status == 403) {
            return UPSTREAM_FORBIDDEN;
        }
        return status;
    }
}
