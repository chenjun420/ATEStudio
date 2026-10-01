"""工位 / 节点 术语不变式。

节点 曾经指两件事, 而只有一件被改名
----------------------------------
* **工位** —— 产线上的物理位置。菜单里曾叫「节点管理」「节点列表」「节点详情」
  「节点流程绑定」。这一层全部改成了工位。
* **流程节点** —— 测试序列图里的一步。规格 §4.2 保留了「流程节点模板」, 因为
  流程编辑器里的节点不是工位: 你在编排序列时选的是「加一步电压测量」, 不是
  「站在 3 号工位」。把两者合并会让流程编排页上出现"你在哪个工位"这种问法。
* **画布节点** —— FixtureDesigner 里 X6 拓扑图的节点(继电器矩阵编辑的对象)。
  同理不是工位。

所以这个不变式断言的是**具体的旧词组**, 不是"源码里没有'节点'两个字"。按字面做
全局替换会把上面三类里该保留的全改掉, 而那种错误不会让任何测试变红 —— 页面照样
渲染, 只是把流程图上的节点叫成了工位。

为什么不变量只覆盖界面文案
--------------------------
代码标识符(``node_flow_bindings`` 端点、``node_templates`` 表、``node:read``
权限)保持原样, 因为它们对着的是数据表与既有接口, 改名要配迁移; 而 ``node:read``
权限字符串在本仓库只存在于种子数据里, 换掉它会让所有非 admin 账号突然看不到菜单
(见 ``test_menu_visibility.py`` 记录的同一个坑)。规格 §4.5 的要求也只写了
「菜单、路由、i18n key、视图名」。

路由里仍有 ``/dev/templates`` 指向 ``NodeTemplates.vue``, 那也是刻意保留的。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend" / "src"

#: Retired labels, mapped to what replaced them. These are whole phrases on
#: purpose — see the module docstring.
RETIRED_PHRASES = {
    "节点管理": "工位运维 / 工位列表",
    "节点列表": "工位列表",
    "节点详情": "工位详情",
    "节点流程绑定": "工位管理",
    "节点流��绑定": "工位管理",
}

#: Must survive. A flow node is a step in the sequence graph; a canvas node is a
#: vertex in the fixture topology. Neither is a physical position.
REQUIRED_PHRASES = {
    "流程节点模板": (
        # The Chinese phrase lives in the locale file and the seed. The frontend
        # label table holds the i18n *key* (`menu.flowNodeTemplates`), so checking
        # the phrase there would be checking the wrong file.
        "frontend/src/i18n/locales/zh-CN.ts",
        "src/ate_cloud/api/v1/apps.py",
    ),
}


def _text_files() -> list[Path]:
    out: list[Path] = []
    for path in FRONTEND.rglob("*"):
        if not path.is_file() or path.suffix not in {".ts", ".vue", ".json"}:
            continue
        if "__pycache__" in path.parts or "node_modules" in path.parts:
            continue
        out.append(path)
    for path in (REPO / "src").rglob("*.py"):
        if "__pycache__" not in path.parts:
            out.append(path)
    return sorted(out)


def _strip_comments(text: str) -> str:
    """Drop line comments and block comments.

    The comments around this change explain the rename in prose and necessarily
    quote the old words — matching raw text fails on the explanation of the fix,
    which is the wrong way round.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"^\s*(?://|///|\*|\#).*$", "", text, flags=re.M)
    text = re.sub(r"(?<![:'\"`/\w])//[^\n]*$", "", text, flags=re.M)
    return text


@pytest.mark.parametrize("phrase", sorted(RETIRED_PHRASES))
def test_retired_station_label_is_gone(phrase: str) -> None:
    """No shipped interface text still says the old word for a station."""
    offenders: list[str] = []
    for path in _text_files():
        body = _strip_comments(path.read_text(encoding="utf-8", errors="ignore"))
        if phrase in body:
            rel = path.relative_to(REPO).as_posix()
            for i, line in enumerate(body.splitlines(), 1):
                if phrase in line:
                    offenders.append(f"{rel}:{i}  {line.strip()[:90]}")
    assert not offenders, (
        f"界面文案里仍有旧术语 {phrase!r}(应改为 {RETIRED_PHRASES[phrase]}):\n"
        + "\n".join(f"  {o}" for o in offenders)
    )


def test_required_flow_node_phrases_survive() -> None:
    """流程节点模板 must still be there.

    A global 节点 → 工位 replacement would remove it and rename the step
    concept in the sequence editor into the station concept. Nothing would go
    red; the page would just ask "which station are you on" while you are
    choosing a measurement step.
    """
    missing: list[str] = []
    for phrase, rel_paths in REQUIRED_PHRASES.items():
        for rel in rel_paths:
            body = _strip_comments((REPO / rel).read_text(encoding="utf-8"))
            if phrase not in body:
                missing.append(f"{phrase} 不在 {rel}")
    assert not missing, "该保留的流程节点术语被误改了:\n" + "\n".join(f"  {m}" for m in missing)


def test_the_flow_node_template_menu_code_and_label_are_unchanged() -> None:
    """`flow-templates` -> `menu.flowNodeTemplates` -> 流程节点模板.

    Three places that have to agree, none of which contains the phrase itself in
    the middle one. Asserted together because the label table is where a rename
    half-applies: the code could change while the phrase stayed, or vice versa,
    and either way the sidebar shows the old word in one locale.
    """
    labels = (FRONTEND / "composables" / "menuLabels.ts").read_text(encoding="utf-8")
    assert "'flow-templates': 'menu.flowNodeTemplates'" in labels, (
        "流程节点模板的菜单 code -> i18n key 映射不见了或被改成了工位"
    )

    zh = (FRONTEND / "i18n" / "locales" / "zh-CN.ts").read_text(encoding="utf-8")
    en = (FRONTEND / "i18n" / "locales" / "en.ts").read_text(encoding="utf-8")
    assert "flowNodeTemplates: '流程节点模板'" in zh
    assert "flowNodeTemplates: 'Flow Node Templates'" in en


def test_the_two_station_pages_have_distinct_names() -> None:
    """工位列表 and 工位执行器 must not be collapsed into one entry.

    They read different tables — ``stations`` (physical positions) and
    ``workers`` (executor processes). They used to share the single label
    「节点列表」 while only one of them was the station registry, so the menu
    claimed a station list that pointed at a worker registry.
    """
    from ate_cloud.api.v1.apps import default_apps

    def walk(menus: list[dict[str, object]]) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for menu in menus:
            out.append(menu)
            out.extend(walk(menu.get("children") or []))  # type: ignore[arg-type]
        return out

    rows = {str(m["code"]): m for a in default_apps for m in walk(a["menus"])}
    assert rows["stations"]["route_path"] == "/ops/stations"
    assert rows["workers"]["route_path"] == "/ops/workers"
    assert rows["stations"]["name"] == "工位列表"
    assert rows["workers"]["name"] == "工位执行器"


def test_station_and_flow_node_are_not_the_same_menu_code() -> None:
    """The router keeps ``NodeTemplates``; the binding page is ``StationManagement``.

    Asserted because the file rename in this change swapped which view file
    holds which concept. A later "cleanup" that unifies the names would silently
    point the 工位管理 menu at the flow-node template view.
    """
    router = (FRONTEND / "router" / "index.ts").read_text(encoding="utf-8")
    assert "name: 'NodeTemplates'" in router
    assert "name: 'StationManagement'" in router
    assert "name: 'WorkerRegistry'" in router
    # And each points at a distinct file.
    assert "'@/views/NodeTemplates.vue'" in router
    assert "'@/views/StationManagement.vue'" in router
    assert "'@/views/WorkerRegistry.vue'" in router
