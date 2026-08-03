<template>
  <div class="welcome">
    <HeaderBar />
    <div class="main-wrapper">
      <div class="content-panel">
        <div class="content-area">
          <el-card class="face-library-card" shadow="never">
            <div class="operation-header">
              <h2 class="page-title">人脸库管理</h2>
              <div class="header-actions">
                <el-button size="small" icon="el-icon-refresh" @click="refreshAll">刷新</el-button>
                <el-button size="small" type="primary" icon="el-icon-plus" @click="showAddSubject">新增人员</el-button>
              </div>
            </div>

            <!-- 仓管系统未启用人脸功能 / 未配置地址：友好提示，不弹错误 -->
            <el-alert v-if="disabledTip" :title="disabledTip" type="info" show-icon :closable="false"
              class="disabled-alert" />

            <div class="body-split">
              <!-- 左：人员档案 -->
              <div class="pane pane-left">
                <div class="pane-title">人员档案</div>
                <el-table :data="subjects" v-loading="subjectLoading" size="small" height="100%" border
                  highlight-current-row @current-change="handleSubjectSelect" empty-text="暂无人员">
                  <el-table-column prop="id" label="ID" width="70" align="center" />
                  <el-table-column prop="name" label="姓名" align="center" />
                  <el-table-column prop="employee_id" label="工号" align="center">
                    <template slot-scope="scope">{{ scope.row.employee_id || '-' }}</template>
                  </el-table-column>
                  <el-table-column prop="enrollment_count" label="人脸数" width="80" align="center" />
                  <el-table-column label="状态" width="80" align="center">
                    <template slot-scope="scope">
                      <el-tag :type="scope.row.is_active ? 'success' : 'info'" size="mini">
                        {{ scope.row.is_active ? '启用' : '停用' }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column label="操作" width="130" align="center">
                    <template slot-scope="scope">
                      <el-button size="mini" type="text" @click.stop="showEditSubject(scope.row)">编辑</el-button>
                      <el-button size="mini" type="text" @click.stop="deleteSubject(scope.row)">删除</el-button>
                    </template>
                  </el-table-column>
                </el-table>
              </div>

              <!-- 右：人脸录入 -->
              <div class="pane pane-right">
                <div class="pane-title">
                  人脸录入
                  <span class="pane-sub" v-if="currentSubject">— {{ currentSubject.name }}</span>
                  <span class="pane-sub" v-else>— 请先在左侧选择人员</span>
                  <el-button class="pane-title-btn" size="mini" type="primary" :disabled="!currentSubject"
                    icon="el-icon-camera" @click="showEnrollDialog">录入人脸</el-button>
                </div>
                <el-table :data="enrollments" v-loading="enrollLoading" size="small" height="100%" border
                  empty-text="暂无人脸录入">
                  <el-table-column prop="id" label="ID" width="70" align="center" />
                  <el-table-column prop="model_tag" label="模型标签" align="center">
                    <template slot-scope="scope">{{ scope.row.model_tag || '-' }}</template>
                  </el-table-column>
                  <el-table-column prop="enrolled_at" label="录入时间" align="center">
                    <template slot-scope="scope">{{ scope.row.enrolled_at || '-' }}</template>
                  </el-table-column>
                  <el-table-column label="状态" width="80" align="center">
                    <template slot-scope="scope">
                      <el-tag :type="scope.row.is_active ? 'success' : 'info'" size="mini">
                        {{ scope.row.is_active ? '有效' : '停用' }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column label="操作" width="90" align="center">
                    <template slot-scope="scope">
                      <el-button size="mini" type="text" @click="deleteEnrollment(scope.row)">删除</el-button>
                    </template>
                  </el-table-column>
                </el-table>
              </div>
            </div>
          </el-card>
        </div>
      </div>
    </div>

    <!-- 人员档案 新增/编辑 -->
    <el-dialog :title="subjectDialogTitle" :visible.sync="subjectDialogVisible" width="460px"
      :close-on-click-modal="false">
      <el-form :model="subjectForm" label-width="80px" size="small">
        <el-form-item label="姓名" required>
          <el-input v-model="subjectForm.name" maxlength="50" placeholder="请输入姓名" />
        </el-form-item>
        <el-form-item label="工号">
          <el-input v-model="subjectForm.employee_id" maxlength="50" placeholder="选填" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="subjectForm.note" type="textarea" :rows="2" maxlength="200" placeholder="选填" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="subjectForm.is_active" />
        </el-form-item>
      </el-form>
      <span slot="footer">
        <el-button size="small" @click="subjectDialogVisible = false">取消</el-button>
        <el-button size="small" type="primary" :loading="subjectSaving" @click="submitSubject">保存</el-button>
      </span>
    </el-dialog>

    <!-- 人脸录入 -->
    <el-dialog title="录入人脸" :visible.sync="enrollDialogVisible" width="560px" :close-on-click-modal="false">
      <el-alert
        title="上传该人员的正面清晰照片，可一次上传多张。图片校验、去重、张数上限与模型一致性均由仓管系统裁决。"
        type="info" :closable="false" show-icon class="enroll-tip" />
      <el-upload action="" list-type="picture-card" :auto-upload="false" :file-list="enrollFileList"
        accept="image/jpeg,image/png" :on-change="handleFileChange" :on-remove="handleFileRemove">
        <i class="el-icon-plus"></i>
      </el-upload>
      <span slot="footer">
        <el-button size="small" @click="enrollDialogVisible = false">取消</el-button>
        <el-button size="small" type="primary" :loading="enrollSaving" :disabled="enrollFileList.length === 0"
          @click="submitEnrollment">提交录入</el-button>
      </span>
    </el-dialog>

    <el-footer>
      <version-footer />
    </el-footer>
  </div>
</template>

<script>
import Api from "@/apis/api";
import HeaderBar from "@/components/HeaderBar.vue";
import VersionFooter from "@/components/VersionFooter.vue";

export default {
  name: "FaceLibrary",
  components: { HeaderBar, VersionFooter },
  data() {
    return {
      subjects: [],
      enrollments: [],
      currentSubject: null,
      subjectLoading: false,
      enrollLoading: false,
      disabledTip: "",

      subjectDialogVisible: false,
      subjectDialogTitle: "新增人员",
      subjectSaving: false,
      subjectForm: { id: null, name: "", employee_id: "", note: "", is_active: true },

      enrollDialogVisible: false,
      enrollSaving: false,
      enrollFileList: []
    };
  },
  mounted() {
    this.fetchSubjects();
  },
  methods: {
    /**
     * 统一的返回处理：code === 0 走成功；404 视为「仓管系统未启用人脸功能」，
     * 只在页面顶部提示，不弹错误框；其余原样回显仓管系统的 msg。
     */
    handleResult(res, onOk, silentContext) {
      if (!res) {
        this.$message.error({ message: "请求失败", showClose: true });
        return;
      }
      if (res.code === 0) {
        this.disabledTip = "";
        onOk && onOk(res.data);
        return;
      }
      if (res.code === 404) {
        this.disabledTip = "仓管系统未启用人脸功能（FACE_ENABLED=false），人脸库管理不可用";
        this.subjects = [];
        this.enrollments = [];
        return;
      }
      if (res.code === 503) {
        this.disabledTip = res.msg || "仓管系统不可达";
        return;
      }
      if (!silentContext) {
        this.$message.error({ message: res.msg || "操作失败", showClose: true });
      }
    },

    refreshAll() {
      this.fetchSubjects();
      if (this.currentSubject) {
        this.fetchEnrollments();
      }
    },

    // ==================== 人员档案 ====================
    fetchSubjects() {
      this.subjectLoading = true;
      Api.faceLibrary.getSubjects({ includeInactive: true }, (res) => {
        this.subjectLoading = false;
        this.handleResult(res, (data) => {
          this.subjects = Array.isArray(data) ? data : [];
          if (this.currentSubject) {
            const still = this.subjects.find((s) => s.id === this.currentSubject.id);
            this.currentSubject = still || null;
            if (!still) this.enrollments = [];
          }
        });
      });
    },

    handleSubjectSelect(row) {
      this.currentSubject = row || null;
      this.enrollments = [];
      if (row) {
        this.fetchEnrollments();
      }
    },

    showAddSubject() {
      this.subjectDialogTitle = "新增人员";
      this.subjectForm = { id: null, name: "", employee_id: "", note: "", is_active: true };
      this.subjectDialogVisible = true;
    },

    showEditSubject(row) {
      this.subjectDialogTitle = "编辑人员";
      this.subjectForm = {
        id: row.id,
        name: row.name || "",
        employee_id: row.employee_id || "",
        note: row.note || "",
        is_active: !!row.is_active
      };
      this.subjectDialogVisible = true;
    },

    submitSubject() {
      if (!this.subjectForm.name || !this.subjectForm.name.trim()) {
        this.$message.warning({ message: "姓名不能为空", showClose: true });
        return;
      }
      const payload = {
        name: this.subjectForm.name.trim(),
        employee_id: this.subjectForm.employee_id || null,
        note: this.subjectForm.note || null,
        is_active: !!this.subjectForm.is_active
      };
      this.subjectSaving = true;
      const done = (res) => {
        this.subjectSaving = false;
        this.handleResult(res, () => {
          this.$message.success({ message: "保存成功", showClose: true });
          this.subjectDialogVisible = false;
          this.fetchSubjects();
        });
      };
      if (this.subjectForm.id) {
        Api.faceLibrary.updateSubject(this.subjectForm.id, payload, done);
      } else {
        Api.faceLibrary.addSubject(payload, done);
      }
    },

    deleteSubject(row) {
      this.$confirm(`确认删除人员「${row.name}」？其名下的人脸录入会一并删除。`, "警告", {
        confirmButtonText: "确定",
        cancelButtonText: "取消",
        type: "warning"
      }).then(() => {
        Api.faceLibrary.deleteSubject(row.id, (res) => {
          this.handleResult(res, () => {
            this.$message.success({ message: "删除成功", showClose: true });
            if (this.currentSubject && this.currentSubject.id === row.id) {
              this.currentSubject = null;
              this.enrollments = [];
            }
            this.fetchSubjects();
          });
        });
      }).catch(() => { });
    },

    // ==================== 人脸录入 ====================
    fetchEnrollments() {
      if (!this.currentSubject) return;
      this.enrollLoading = true;
      Api.faceLibrary.getEnrollments({ subjectId: this.currentSubject.id }, (res) => {
        this.enrollLoading = false;
        this.handleResult(res, (data) => {
          this.enrollments = Array.isArray(data) ? data : [];
        });
      });
    },

    showEnrollDialog() {
      this.enrollFileList = [];
      this.enrollDialogVisible = true;
    },

    handleFileChange(file, fileList) {
      this.enrollFileList = fileList;
    },

    handleFileRemove(file, fileList) {
      this.enrollFileList = fileList;
    },

    readAsBase64(rawFile) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
          const result = String(reader.result || "");
          // 只取 base64 主体，去掉 data:image/...;base64, 前缀
          const idx = result.indexOf("base64,");
          resolve(idx >= 0 ? result.slice(idx + 7) : result);
        };
        reader.onerror = reject;
        reader.readAsDataURL(rawFile);
      });
    },

    async submitEnrollment() {
      if (!this.currentSubject || this.enrollFileList.length === 0) return;
      this.enrollSaving = true;
      try {
        const images = [];
        for (const f of this.enrollFileList) {
          const raw = f.raw || f;
          images.push(await this.readAsBase64(raw));
        }
        Api.faceLibrary.addEnrollment(
          { subject_id: this.currentSubject.id, images_b64: images },
          (res) => {
            this.enrollSaving = false;
            this.handleResult(res, () => {
              this.$message.success({ message: "录入成功", showClose: true });
              this.enrollDialogVisible = false;
              this.enrollFileList = [];
              this.fetchEnrollments();
              this.fetchSubjects();
            });
          }
        );
      } catch (e) {
        this.enrollSaving = false;
        this.$message.error({ message: "图片读取失败", showClose: true });
      }
    },

    deleteEnrollment(row) {
      this.$confirm("确认删除这条人脸录入？", "警告", {
        confirmButtonText: "确定",
        cancelButtonText: "取消",
        type: "warning"
      }).then(() => {
        Api.faceLibrary.deleteEnrollment(row.id, (res) => {
          this.handleResult(res, () => {
            this.$message.success({ message: "删除成功", showClose: true });
            this.fetchEnrollments();
            this.fetchSubjects();
          });
        });
      }).catch(() => { });
    }
  }
};
</script>

<style lang="scss" scoped>
.welcome {
  min-width: 900px;
  min-height: 506px;
  height: 100vh;
  display: flex;
  position: relative;
  flex-direction: column;
  background-size: cover;
  background: #eff4ff;
  -webkit-background-size: cover;
  -o-background-size: cover;
  overflow: hidden;
}

.main-wrapper {
  height: calc(100vh - 63px - 35px);
  padding: 20px 22px 0;
  position: relative;
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
}

.operation-header {
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 0 16px 0;
  box-sizing: border-box;
}

.page-title {
  font-weight: 500;
  font-size: 24px;
  margin: 0;
}

.content-panel {
  display: flex;
  overflow: hidden;
  height: 100%;
  border-radius: 15px;
  background: transparent;
  border: 1px solid #fff;
}

.content-area {
  flex: 1;
  height: 100%;
  min-width: 600px;
  overflow: auto;
  background-color: white;
  display: flex;
  flex-direction: column;
}

.face-library-card {
  background: white;
  flex: 1;
  display: flex;
  flex-direction: column;
  border: none;
  box-shadow: none;
  overflow: hidden;

  ::v-deep .el-card__body {
    padding: 14px 20px;
    display: flex;
    flex-direction: column;
    flex: 1;
    overflow: hidden;
  }
}

.disabled-alert {
  margin-bottom: 12px;
}

.body-split {
  flex: 1;
  display: flex;
  gap: 16px;
  overflow: hidden;
}

.pane {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.pane-left {
  flex: 1.1;
  min-width: 380px;
}

.pane-right {
  flex: 1;
  min-width: 340px;
}

.pane-title {
  font-size: 15px;
  font-weight: 500;
  padding-bottom: 8px;
  display: flex;
  align-items: center;
}

.pane-sub {
  margin-left: 6px;
  font-size: 13px;
  font-weight: 400;
  color: #909399;
}

.pane-title-btn {
  margin-left: auto;
}

.enroll-tip {
  margin-bottom: 12px;
}

:deep(.el-table .el-button--text) {
  color: #7079aa;
}

:deep(.el-table .el-button--text:hover) {
  color: #5a64b5;
}
</style>
