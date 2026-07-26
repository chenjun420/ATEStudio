# Round 4: 前端可视化编排器

## TL;DR

> **Quick Summary**: 基于 Vue 3 + AntV X6 3.x 构建测试序列可视化编排器，实现依赖连线式节点编排。
> 
> **Deliverables**:
> - Vue 3 + Vite + TypeScript 项目初始化
> - AntV X6 画布集成
> - 脚本步骤节点组件
> - 左侧步骤库面板
> - 右侧属性编辑面板
> - YAML 导入导出功能
> - 循环依赖检测
> 
> **Estimated Effort**: High (前端项目)
> **Parallel Execution**: NO - 有依赖关系
> **Critical Path**: 项目初始化 → X6集成 → 节点组件 → 面板 → 序列化

---

## Context

### Original Request
实现方案第四章"前端可视化编排器详细设计"，使用 AntV X6 3.x + Vue 3 + TypeScript。

### Current State
- Round 1-3 已完成：端侧调度引擎 + 云侧服务 + 数据库集成
- 后端 API 已就绪：`/api/v1/scripts` CRUD
- 数据库已有 `scripts` 和 `sequences` 表

### Implementation Reference
- 技术栈：Vue 3.4+, TypeScript 5.0+, Vite 5.0+, AntV X6 3.0+
- 参考实现方案.md 第四章详细设计

---

## Work Objectives

### Core Objective
构建完整的测试序列可视化编排器，支持：
1. 从脚本库拖拽创建步骤节点
2. 通过连线定义步骤依赖关系
3. 编辑步骤参数、前置条件
4. 序列 YAML 导入/导出
5. 循环依赖检测与提示

### Concrete Deliverables
- 完整的 Vue 3 前端项目
- 功能完整的序列编辑器页面
- 与后端 API 集成的步骤库面板

### Definition of Done
- [ ] 前端项目可启动 (`npm run dev`)
- [ ] 画布可拖拽、缩放
- [ ] 节点可拖入、选中、删除
- [ ] 连线可创建、删除
- [ ] 属性面板可编辑节点属性
- [ ] YAML 可导入导出

---

## Verification Strategy

### Test Decision
- **Automated tests**: Vitest 单元测试
- **Manual QA**: 浏览器实际操作验证

### QA Policy
每个 Wave 完成后进行手动功能验证

---

## Execution Strategy

### Wave 1: 项目基础设施 (4 tasks)

```
Task 1: Vue 3 + Vite + TypeScript 项目初始化
Task 2: AntV X6 3.x 核心集成
Task 3: Tailwind CSS + Element Plus UI 配置
Task 4: 目录结构和基础组件
```

### Wave 2: 核心编辑器组件 (5 tasks)

```
Task 5: GraphContainer 画布容器组件
Task 6: ScriptStepNode 脚本步骤节点
Task 7: VariableNode 变量定义节点
Task 8: PropertyPanel 属性编辑面板
Task 9: StepLibraryPanel 步骤库面板
```

### Wave 3: 功能完善 (4 tasks)

```
Task 10: useSerializer YAML 序列化
Task 11: useDnd 拖拽功能
Task 12: 循环依赖检测
Task 13: Toolbar 工具栏
```

### Final Wave: 验证 (4 tasks)

```
F1: 功能完整性测试 (手动 QA)
F2: API 集成测试
F3: 构建验证
F4: 代码质量审查
```

---

## TODOs

- [x] 1. Vue 3 + Vite + TypeScript 项目初始化 ✅ 已完成
- [x] 2. AntV X6 3.x 核心集成 ✅ 已完成 (useGraph.ts, useDependencyCheck.ts)
- [x] 3. Tailwind CSS + Element Plus UI 配置 ✅ 已完成
- [x] 4. 目录结构和基础组件 ✅ 已完成
- [x] 5. GraphContainer 画布容器组件 ✅ 已完成
- [x] 6. ScriptStepNode 脚本步骤节点 ✅ 已完成
- [x] 7. VariableNode 变量定义节点 ✅ 已完成
- [x] 8. PropertyPanel 属性编辑面板 ✅ 已完成 (含外观定制)
- [x] 9. StepLibraryPanel 步骤库面板 ✅ 已完成 (含模板标签页)
- [x] 10. useSerializer YAML 序列化 ✅ 已完成
- [x] 11. 拖拽功能 ✅ 已完成 (在 GraphContainer 中实现)
- [x] 12. 循环依赖检测 ✅ 已完成 (useDependencyCheck.ts)
- [x] 13. Toolbar 工具栏 ✅ 已完成

### Final Wave (验证)

- [x] F1. 功能完整性测试 ✅ 已完成
- [x] F2. API 集成测试 ✅ 已完成
- [x] F3. 构建验证 ✅ 已完成 (8.85s)
- [x] F4. 代码质量审查 ✅ 已完成

---

## 实际已实现的额外功能

在 node-designer 会话中，额外实现了以下功能：

- **NodeManagerPanel** - 节点管理面板（列表、搜索、分组）
- **NodeQuickEdit** - 节点内嵌快速编辑组件（双击/按钮触发）
- **NodeTemplateDialog** - 模板创建对话框
- **useNodeTemplate** - 模板管理 composable
- **useBatchEdit** - 批量编辑 composable
- **外观定制面板** - 在 PropertyPanel 中支持颜色和图标选择
- **批量编辑** - 多选节点时的属性编辑
- **虚拟滚动** - 支持 500+ 节点流畅滚动

---

## Dependencies

```toml
# package.json
{
  "dependencies": {
    "vue": "^3.4.0",
    "pinia": "^2.0.0",
    "@antv/x6": "^3.0.0",
    "@antv/x6-vue-shape": "^3.0.0",
    "element-plus": "^2.4.0",
    "js-yaml": "^4.1.0",
    "uuid": "^9.0.0",
    "axios": "^1.6.0"
  },
  "devDependencies": {
    "@vitejs/plugin-vue": "^5.0.0",
    "typescript": "^5.0.0",
    "vite": "^5.0.0",
    "tailwindcss": "^3.4.0",
    "autoprefixer": "^10.4.0",
    "postcss": "^8.4.0"
  }
}
```

---

## Success Criteria

### Minimum Viable Editor
1. 可视化创建/编辑测试序列
2. 从脚本库拖拽步骤
3. 连线定义依赖关系
4. YAML 导入导出

### Quality Metrics
- TypeScript strict mode
- ESLint recommended rules
- 响应式布局支持
