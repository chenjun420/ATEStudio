# 节点设计器和管理功能增强

## TL;DR

> **Quick Summary**: 在现有序列编辑器页面增强节点设计能力，实现节点内嵌快速编辑、外观定制、自定义模板和节点管理面板。
> 
> **Deliverables**:
> - 节点内嵌快速属性编辑（双击/按钮触发）
> - 节点外观定制面板（颜色、图标）
> - 自定义节点模板系统（后端 API 存储）
> - 节点管理面板（列表、搜索、分组、批量操作）
> - 批量编辑功能（多选节点属性编辑）
> 
> **Estimated Effort**: Medium
> **Parallel Execution**: YES - 3 Waves
> **Critical Path**: 基础设施 → 节点管理面板 → 模板系统

---

## Context

### Original Request
在 `http://localhost:5173/sequence` 页面增加节点设计器和管理功能，点击节点支持配置节点属性。

### Current State
- ✅ Vue 3 + Vite + TypeScript 项目已就绪
- ✅ AntV X6 画布已集成
- ✅ PropertyPanel 已实现 ScriptStep/Variable 属性编辑
- ✅ Selection 插件支持多选、框选
- ✅ 节点拖拽创建功能已实现

### Interview Summary
**Key Discussions**:
- 展示形式：右侧固定面板 + 节点内嵌表单（双击或按钮触发）
- 节点设计器：属性配置 + 外观定制 + 自定义模板
- 节点管理：列表查看 + 搜索过滤 + 分组 + 批量操作
- 数据存储：后端 API 存储
- 节点规模：100-500 个节点，需要虚拟滚动优化
- 多选行为：批量编辑共有属性

### Metis Review
**Identified Gaps** (addressed):
- 数据存储策略：确认使用后端 API
- 性能边界：100-500 节点，需虚拟滚动
- 多选编辑行为：批量编辑共有属性
- 触发方式：双击 + 按钮双入口

---

## Work Objectives

### Core Objective
增强现有序列编辑器，实现完整的节点设计和管理能力。

### Concrete Deliverables
- `NodeQuickEdit.vue` - 节点内嵌快速编辑组件
- `NodeAppearancePanel.vue` - 外观定制面板（在 PropertyPanel 中）
- `NodeManagerPanel.vue` - 节点管理面板
- `NodeTemplateDialog.vue` - 模板创建/使用对话框
- `useNodeTemplate.ts` - 模板管理 composable
- `useBatchEdit.ts` - 批量编辑 composable
- 后端 API 扩展（模板 CRUD）

### Definition of Done
- [ ] 双击节点显示快速编辑表单
- [ ] 节点编辑按钮可触发快速编辑
- [ ] PropertyPanel 支持外观定制
- [ ] 节点模板可创建、保存、使用
- [ ] NodeManagerPanel 显示节点列表
- [ ] 搜索过滤功能正常
- [ ] 分组功能可用
- [ ] 批量删除/移动/应用模板可用
- [ ] 多选时显示批量编辑面板

### Must Have
- 节点内嵌快速编辑（双击 + 按钮）
- 外观定制（颜色、图标）
- 节点管理面板（列表 + 搜索 + 分组）
- 批量编辑功能

### Must NOT Have (Guardrails)
- ❌ 节点市场/商店功能
- ❌ 节点版本控制
- ❌ 用户权限管理
- ❌ 复杂拖拽排序
- ❌ 节点关联分析/推荐系统

---

## Verification Strategy

### Test Decision
- **Infrastructure exists**: Vitest
- **Automated tests**: Tests-after（主要功能后测试）
- **Framework**: Vitest + Vue Test Utils
- **Agent-Executed QA**: Playwright 浏览器测试

### QA Policy
每个功能模块完成后进行 Playwright 自动化测试。

---

## Execution Strategy

### Wave 1: 基础设施 (3 tasks)
```
Task 1: 节点模板数据模型和 API
Task 2: 节点分组数据模型
Task 3: useNodeTemplate composable
```

### Wave 2: 节点管理面板 (4 tasks)
```
Task 4: NodeManagerPanel 组件
Task 5: 节点列表虚拟滚动
Task 6: 搜索过滤功能
Task 7: 分组管理功能
```

### Wave 3: 节点编辑增强 (5 tasks)
```
Task 8: 节点内嵌快速编辑组件
Task 9: 节点外观定制面板
Task 10: 模板创建对话框
Task 11: 模板应用功能
Task 12: 批量编辑功能
```

### Final Verification Wave (4 tasks)
```
F1: 功能完整性测试
F2: 性能测试（500节点）
F3: API 集成测试
F4: 代码质量审查
```

---

## TODOs

### Wave 1: 基础设施

- [x] 1. 节点模板数据模型和 API

  **What to do**:
  - 创建后端 `NodeTemplate` 数据模型（name, type, appearance, defaultData）
  - 创建 `/api/v1/node-templates` CRUD API
  - 创建前端 `api/nodeTemplates.ts` API 客户端

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES (with Task 2)
  - **Blocked By**: None

  **References**:
  - `frontend/src/api/scripts.ts` - API 客户端模式参考
  - `src/ate_cloud/models/script.py` - 后端模型参考

  **Acceptance Criteria**:
  - [ ] 后端 NodeTemplate 模型可创建/读取/更新/删除
  - [ ] 前端 API 客户端可调用

  **QA Scenarios**:
  ```
  Scenario: 模板 API CRUD 测试
    Tool: Bash (curl)
    Steps:
      1. curl -X POST http://localhost:8000/api/v1/node-templates -d '{"name":"Test","type":"script-step"}'
      2. curl http://localhost:8000/api/v1/node-templates
      3. curl -X DELETE http://localhost:8000/api/v1/node-templates/{id}
    Expected Result: CRUD 操作成功
    Evidence: .sisyphus/evidence/task-01-template-api.txt
  ```

- [x] 2. 节点分组数据模型

  **What to do**:
  - 扩展节点数据结构，添加 `groupId` 字段
  - 创建 `NodeGroup` 类型定义
  - 在 Sequence 数据中存储分组信息

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES (with Task 1)

  **References**:
  - `frontend/src/models/nodes/types.ts` - 节点类型定义

  **Acceptance Criteria**:
  - [ ] 节点数据包含 groupId 字段
  - [ ] NodeGroup 类型定义完整

- [x] 3. useNodeTemplate composable

  **What to do**:
  - 创建 `frontend/src/composables/useNodeTemplate.ts`
  - 实现 `loadTemplates()`, `createTemplate()`, `applyTemplate()` 函数
  - 实现模板缓存

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: [`vue-best-practices`]

  **Parallelization**:
  - **Blocked By**: Task 1

  **References**:
  - `frontend/src/composables/useSerializer.ts` - Composable 模式参考

  **Acceptance Criteria**:
  - [ ] 可加载模板列表
  - [ ] 可创建新模板
  - [ ] 可应用模板到节点

### Wave 2: 节点管理面板

- [x] 4. NodeManagerPanel 组件

  **What to do**:
  - 创建 `frontend/src/views/SequenceEditor/components/NodeManagerPanel.vue`
  - 实现节点列表展示（使用 inject graphInstance）
  - 实现节点选择同步（点击列表项选中画布节点）

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Skills**: [`vue-best-practices`, `frontend-ui-ux`]

  **Parallelization**:
  - **Blocked By**: None

  **References**:
  - `frontend/src/views/SequenceEditor/components/PropertyPanel.vue` - 面板模式参考

  **Acceptance Criteria**:
  - [ ] 显示当前画布所有节点列表
  - [ ] 点击列表项选中对应节点
  - [ ] 实时同步节点变化

- [x] 5. 节点列表虚拟滚动

  **What to do**:
  - 使用 `vue-virtual-scroller` 或 Element Plus VirtualScroll
  - 实现 500+ 节点流畅滚动
  - 优化渲染性能

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Skills**: [`vue-best-practices`]

  **Parallelization**:
  - **Blocked By**: Task 4

  **Acceptance Criteria**:
  - [ ] 500 节点滚动流畅（< 16ms/frame）
  - [ ] 无明显卡顿

- [x] 6. 搜索过滤功能

  **What to do**:
  - 实现搜索输入框
  - 支持按节点 ID、名称、类型搜索
  - 实现实时过滤

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Skills**: [`vue-best-practices`]

  **Parallelization**:
  - **Blocked By**: Task 4

  **Acceptance Criteria**:
  - [ ] 输入搜索词实时过滤节点
  - [ ] 无结果时显示空状态

  **QA Scenarios**:
  ```
  Scenario: 搜索过滤测试
    Tool: Playwright
    Steps:
      1. page.goto('http://localhost:5173/sequence')
      2. page.fill('.node-manager-search input', 'step-001')
      3. Expect node list shows only matching nodes
      4. page.fill('.node-manager-search input', 'nonexistent')
      5. Expect empty state message displayed
    Expected Result: 搜索实时过滤节点列表
    Evidence: .sisyphus/evidence/task-06-search.png
  ```

- [x] 7. 分组管理功能

  **What to do**:
  - 实现分组创建/删除/重命名
  - 实现节点拖入/拖出分组
  - 实现分组折叠/展开

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Skills**: [`vue-best-practices`, `frontend-ui-ux`]

  **Parallelization**:
  - **Blocked By**: Task 4, Task 2

  **Acceptance Criteria**:
  - [ ] 可创建/删除分组
  - [ ] 可将节点加入分组
  - [ ] 分组可折叠

### Wave 3: 节点编辑增强

- [x] 8. 节点内嵌快速编辑组件

  **What to do**:
  - 创建 `frontend/src/views/SequenceEditor/components/NodeQuickEdit.vue`
  - 实现双击节点显示编辑表单
  - 实现节点编辑按钮触发
  - 支持编辑 stepId、主要参数

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Skills**: [`vue-best-practices`, `frontend-ui-ux`]

  **Parallelization**:
  - **Blocked By**: None

  **References**:
  - `frontend/src/views/SequenceEditor/components/GraphContainer.vue` - 添加双击事件监听
  - AntV X6 node:dblclick 事件

  **Acceptance Criteria**:
  - [ ] 双击节点显示快速编辑表单
  - [ ] 点击编辑按钮显示表单
  - [ ] 编辑后数据同步到节点

  **QA Scenarios**:
  ```
  Scenario: 双击快速编辑测试
    Tool: Playwright
    Steps:
      1. page.goto('http://localhost:5173/sequence')
      2. page.dblclick('.x6-node')
      3. Expect quick edit form visible
      4. page.fill('input[name="stepId"]', 'new-step-id')
      5. page.click('button[type="submit"]')
      6. Expect node data updated
    Expected Result: 快速编辑表单显示并工作
    Evidence: .sisyphus/evidence/task-08-quick-edit.png
  ```

- [x] 9. 节点外观定制面板

  **What to do**:
  - 在 PropertyPanel 中添加外观定制区域
  - 实现颜色选择器
  - 实现图标选择器
  - 实时预览外观变化

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Skills**: [`vue-best-practices`, `frontend-ui-ux`]

  **Parallelization**:
  - **Can Run In Parallel**: YES (with Task 8)

  **References**:
  - `frontend/src/views/SequenceEditor/components/PropertyPanel.vue`

  **Acceptance Criteria**:
  - [ ] 可选择节点颜色
  - [ ] 可选择节点图标
  - [ ] 变化实时预览

- [x] 10. 模板创建对话框

  **What to do**:
  - 创建 `frontend/src/views/SequenceEditor/components/NodeTemplateDialog.vue`
  - 实现从现有节点创建模板
  - 支持设置模板名称、描述
  - 保存到后端 API

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Skills**: [`vue-best-practices`, `frontend-ui-ux`]

  **Parallelization**:
  - **Blocked By**: Task 3

  **Acceptance Criteria**:
  - [ ] 可从节点创建模板
  - [ ] 模板保存到后端

- [x] 11. 模板应用功能

  **What to do**:
  - 实现从模板创建节点
  - 在 StepLibraryPanel 中显示模板分类
  - 支持拖拽模板到画布

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Skills**: [`vue-best-practices`]

  **Parallelization**:
  - **Blocked By**: Task 10

  **Acceptance Criteria**:
  - [ ] 可选择模板创建节点
  - [ ] 模板节点具有预设外观和属性

  **QA Scenarios**:
  ```
  Scenario: 模板应用测试
    Tool: Playwright
    Steps:
      1. page.goto('http://localhost:5173/sequence')
      2. page.click('.step-library-panel .templates-tab')
      3. page.dragAndDrop('.template-item[data-id="template-1"]', '.graph-container')
      4. Expect new node created with template appearance
      5. Expect node has preset properties from template
    Expected Result: 模板可拖拽创建节点并应用预设
    Evidence: .sisyphus/evidence/task-11-template-apply.png
  ```

- [x] 12. 批量编辑功能

  **What to do**:
  - 创建 `frontend/src/composables/useBatchEdit.ts`
  - 实现多选时 PropertyPanel 显示批量编辑模式
  - 支持编辑共有属性（如 timeout、onFail）
  - 支持批量应用模板

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Skills**: [`vue-best-practices`]

  **Parallelization**:
  - **Blocked By**: Task 8

  **References**:
  - `frontend/src/views/SequenceEditor/components/PropertyPanel.vue`

  **Acceptance Criteria**:
  - [ ] 多选时显示批量编辑面板
  - [ ] 可修改共有属性
  - [ ] 批量删除/移动可用

  **QA Scenarios**:
  ```
  Scenario: 批量编辑测试
    Tool: Playwright
    Steps:
      1. Select multiple nodes (Ctrl+click)
      2. Expect PropertyPanel shows batch edit mode
      3. Change timeout to 60000
      4. Click Apply
      5. Expect all selected nodes updated
    Expected Result: 批量编辑成功
    Evidence: .sisyphus/evidence/task-12-batch-edit.png
  ```

### Final Verification Wave

- [x] F1. 功能完整性测试

  **What to do**:
  - Playwright 自动化测试所有功能
  - 验证完整用户流程

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: []

  **QA Scenarios**:
  ```
  Scenario: 完整用户流程测试
    Tool: Playwright
    Steps:
      1. page.goto('http://localhost:5173/sequence')
      2. Drag script from library to canvas
      3. Double-click node to edit
      4. Change appearance in PropertyPanel
      5. Create template from node
      6. Search nodes in manager panel
      7. Select multiple nodes and batch edit
      8. Export sequence as YAML
    Expected Result: 所有功能工作正常，无错误
    Evidence: .sisyphus/evidence/F1-full-flow.png
  ```

- [x] F2. 性能测试（500节点）

  **What to do**:
  - 创建 500 节点测试场景
  - 测试滚动、搜索、批量操作性能

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: []

  **QA Scenarios**:
  ```
  Scenario: 500节点性能测试
    Tool: Playwright
    Steps:
      1. Load sequence with 500 nodes
      2. Measure scroll FPS (expect > 30fps)
      3. Search for node (expect < 100ms)
      4. Select all nodes (expect < 500ms)
      5. Batch delete 100 nodes (expect < 1s)
    Expected Result: 所有操作在可接受时间内完成
    Evidence: .sisyphus/evidence/F2-performance.txt
  ```

- [x] F3. API 集成测试

  **What to do**:
  - 测试前后端 API 通信
  - 验证模板 CRUD

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: []

  **QA Scenarios**:
  ```
  Scenario: API集成测试
    Tool: Bash (curl)
    Steps:
      1. curl -X GET http://localhost:8000/api/v1/node-templates
      2. curl -X POST http://localhost:8000/api/v1/node-templates -d '{"name":"Test","type":"script-step"}'
      3. curl -X PUT http://localhost:8000/api/v1/node-templates/{id} -d '{"name":"Updated"}'
      4. curl -X DELETE http://localhost:8000/api/v1/node-templates/{id}
    Expected Result: 所有 API 调用返回正确状态码
    Evidence: .sisyphus/evidence/F3-api.txt
  ```

- [x] F4. 代码质量审查

  **What to do**:
  - ESLint + TypeScript 检查
  - 代码结构审查

  **Recommended Agent Profile**:
  - **Category**: `deep`
  - **Skills**: []

  **QA Scenarios**:
  ```
  Scenario: 代码质量检查
    Tool: Bash
    Steps:
      1. cd frontend && npm run lint
      2. cd frontend && npx tsc --noEmit
      3. Check for AI slop patterns (excessive comments, over-abstraction)
    Expected Result: Lint 无错误，TypeScript 编译通过
    Evidence: .sisyphus/evidence/F4-quality.txt
  ```

---

## Dependencies

### Frontend
```json
{
  "dependencies": {
    "vue-virtual-scroller": "^2.0.0"
  }
}
```

### Backend
```python
# 新增模型
class NodeTemplate(Base):
    id: str
    name: str
    type: str  # script-step, variable, etc.
    appearance: JSON  # { color, icon }
    default_data: JSON  # 默认属性值
```

---

## Success Criteria

### Verification Commands
```bash
# 启动前端
cd frontend && npm run dev

# 启动后端
uv run uvicorn ate_cloud.main:app --reload

# 测试
npm run test
```

### Final Checklist
- [ ] 双击节点显示快速编辑
- [ ] 外观定制可用
- [ ] 模板创建/应用可用
- [ ] 节点管理面板可用
- [ ] 搜索/分组可用
- [ ] 批量操作可用
- [ ] 500 节点性能 OK
