import { getServiceUrl } from '../api';
import RequestService from '../httpRequest';

/**
 * 人脸库管理 —— manager-api /face/* 薄代理，业务逻辑全在仓管系统。
 * 所有回调统一收到 manager-api 的 Result（{ code, msg, data }），
 * code === 404 表示仓管系统未启用人脸功能（FACE_ENABLED=false）。
 */
export default {
    // ==================== 人员档案 ====================
    getSubjects(params, callback) {
        const query = new URLSearchParams();
        if (params && params.tenantId != null) query.append('tenantId', params.tenantId);
        if (params && params.includeInactive != null) query.append('includeInactive', params.includeInactive);
        const qs = query.toString();
        RequestService.sendRequest()
            .url(`${getServiceUrl()}/face/subjects${qs ? '?' + qs : ''}`)
            .method('GET')
            .success((res) => {
                RequestService.clearRequestTime();
                callback(res.data);
            })
            .fail((res) => {
                callback(res.data || { code: -1, msg: '请求失败' });
            })
            .networkFail((err) => {
                console.error('获取人员档案失败:', err);
                callback({ code: -1, msg: '网络异常，无法连接管理后台' });
            }).send();
    },

    addSubject(payload, callback) {
        RequestService.sendRequest()
            .url(`${getServiceUrl()}/face/subjects`)
            .method('POST')
            .data(JSON.stringify(payload))
            .success((res) => {
                RequestService.clearRequestTime();
                callback(res.data);
            })
            .fail((res) => {
                callback(res.data || { code: -1, msg: '请求失败' });
            })
            .networkFail((err) => {
                console.error('新增人员档案失败:', err);
                callback({ code: -1, msg: '网络异常，无法连接管理后台' });
            }).send();
    },

    updateSubject(subjectId, payload, callback) {
        RequestService.sendRequest()
            .url(`${getServiceUrl()}/face/subjects/${subjectId}`)
            .method('PUT')
            .data(JSON.stringify(payload))
            .success((res) => {
                RequestService.clearRequestTime();
                callback(res.data);
            })
            .fail((res) => {
                callback(res.data || { code: -1, msg: '请求失败' });
            })
            .networkFail((err) => {
                console.error('修改人员档案失败:', err);
                callback({ code: -1, msg: '网络异常，无法连接管理后台' });
            }).send();
    },

    deleteSubject(subjectId, callback) {
        RequestService.sendRequest()
            .url(`${getServiceUrl()}/face/subjects/${subjectId}`)
            .method('DELETE')
            .success((res) => {
                RequestService.clearRequestTime();
                callback(res.data);
            })
            .fail((res) => {
                callback(res.data || { code: -1, msg: '请求失败' });
            })
            .networkFail((err) => {
                console.error('删除人员档案失败:', err);
                callback({ code: -1, msg: '网络异常，无法连接管理后台' });
            }).send();
    },

    // ==================== 人脸录入 ====================
    getEnrollments(params, callback) {
        const query = new URLSearchParams();
        if (params && params.subjectId != null) query.append('subjectId', params.subjectId);
        if (params && params.tenantId != null) query.append('tenantId', params.tenantId);
        const qs = query.toString();
        RequestService.sendRequest()
            .url(`${getServiceUrl()}/face/enrollments${qs ? '?' + qs : ''}`)
            .method('GET')
            .success((res) => {
                RequestService.clearRequestTime();
                callback(res.data);
            })
            .fail((res) => {
                callback(res.data || { code: -1, msg: '请求失败' });
            })
            .networkFail((err) => {
                console.error('获取人脸录入失败:', err);
                callback({ code: -1, msg: '网络异常，无法连接管理后台' });
            }).send();
    },

    /**
     * 新增人脸录入。
     * payload: { subject_id, images_b64: [...], applies_to_warehouse_ids?: [...] }
     * 图片编码/去重/上限/model_tag 一致性全部由仓管系统裁决，前端不做任何业务判断。
     */
    addEnrollment(payload, callback) {
        RequestService.sendRequest()
            .url(`${getServiceUrl()}/face/enrollments`)
            .method('POST')
            .data(JSON.stringify(payload))
            .success((res) => {
                RequestService.clearRequestTime();
                callback(res.data);
            })
            .fail((res) => {
                callback(res.data || { code: -1, msg: '请求失败' });
            })
            .networkFail((err) => {
                console.error('新增人脸录入失败:', err);
                callback({ code: -1, msg: '网络异常，无法连接管理后台' });
            }).send();
    },

    deleteEnrollment(enrollmentId, callback) {
        RequestService.sendRequest()
            .url(`${getServiceUrl()}/face/enrollments/${enrollmentId}`)
            .method('DELETE')
            .success((res) => {
                RequestService.clearRequestTime();
                callback(res.data);
            })
            .fail((res) => {
                callback(res.data || { code: -1, msg: '请求失败' });
            })
            .networkFail((err) => {
                console.error('删除人脸录入失败:', err);
                callback({ code: -1, msg: '网络异常，无法连接管理后台' });
            }).send();
    }
};
