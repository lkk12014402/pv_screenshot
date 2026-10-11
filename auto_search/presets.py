"""内置的「药物 × 数据库」检索式预设（来源: search.md）。

GUI 中选择 药物 -> 数据库 后自动填入对应检索式；仅作预填，用户可再手动修改。
数据库键与 config.yaml 的 tasks[].database 对应；万方/知网的自动流程暂未实现，
预设仅用于预填检索式。
"""
from __future__ import annotations

# 数据库内部键 -> 界面显示名（顺序即下拉框顺序）
DATABASE_LABELS: dict[str, str] = {
    "pubmed": "PubMed",
    "embase": "Embase",
    "wanfang": "万方",
    "cnki": "知网",
}

# 药物(成分) -> {数据库键: 检索式}
DRUG_PRESETS: dict[str, dict[str, str]] = {
    "68Ga / GalenVita": {
        "pubmed": '("gallium 68"[mh] OR "gallium radioisotopes"[mh] OR "generator"[tiab] OR "68Ga Generator"[tiab] OR GalenVita[tiab] OR "gallium 68 plus germanium 68"[tiab] OR ("gallium 68"[tiab] AND generator[tiab])) AND ("adverse effects"[Subheading] OR "drug-related side effects and adverse reactions"[mh] OR adverse[tiab] OR side effect[tiab] OR toxicity[tiab] OR safety[tiab]) AND ("China"[Affiliation] OR "Chinese"[Affiliation])',
        "embase": "('gallium 68'/exp OR 'gallium generator'/exp OR 'gallium 68 plus germanium 68':ti,ab,kw OR 'gallium 68 generator':ti,ab,kw OR ('gallium 68':ti,ab,kw AND generator:ti,ab,kw) OR galenvita:ti,ab,kw) AND ('adverse drug reaction'/exp OR 'side effect'/exp OR 'drug toxicity'/exp OR adverse:ti,ab,kw OR 'side effect':ti,ab,kw OR toxicity:ti,ab,kw OR safety:ti,ab,kw) AND 'china':af",
        "wanfang": "主题:(镓68 OR 68Ga OR 镓发生器 OR 锗镓发生器 OR GalenVita OR 68Ga发生器) and 主题:(不良事件 OR 不良反应 OR 副作用 OR 毒性 OR 安全性)",
        "cnki": "SU=('镓68'+'68Ga'+'镓发生器'+'锗镓发生器'+'GalenVita'+'68Ga发生器')*('不良事件'+'不良反应'+'副作用'+'毒性'+'安全性')",
    },
    "NCA Lu 177 / 氯化镥[¹⁷⁷Lu]": {
        "pubmed": '("lutetium"[mh] OR "lutetium compounds"[mh] OR "Lutetium Lu 177"[Supplementary Concept] OR "lutetium chloride lu 177"[tiab] OR "lu177 chloride"[tiab] OR "lu-chloride"[tiab] OR "lutetium nca"[tiab] OR lutenca[tiab] OR lunca[tiab] OR "mon Lutec"[tiab] OR monlutenca[tiab] OR "NCA Lu-177"[tiab]) AND ("adverse effects"[Subheading] OR "drug-related side effects and adverse reactions"[mh] OR adverse[tiab] OR side effect[tiab] OR toxicity[tiab] OR safety[tiab]) AND ("China"[Affiliation] OR "Chinese"[Affiliation])',
        "embase": "('lutetium lu 177'/exp OR 'lutetium chloride lu 177':ti,ab,kw OR 'lu177 chloride':ti,ab,kw OR 'lu-chloride':ti,ab,kw OR 'lutetium nca':ti,ab,kw OR lutenca:ti,ab,kw OR lunca:ti,ab,kw OR 'mon Lutec':ti,ab,kw OR monlutenca:ti,ab,kw OR 'NCA Lu-177':ti,ab,kw) AND ('adverse drug reaction'/exp OR 'side effect'/exp OR 'drug toxicity'/exp OR adverse:ti,ab,kw OR 'side effect':ti,ab,kw OR toxicity:ti,ab,kw OR safety:ti,ab,kw) AND 'china':af",
        "wanfang": "主题:(镥177 OR 氯化镥177 OR Lu-177 OR 177Lu OR 无载体镥177 OR NCA Lu-177 OR lutenca OR lunca) and 主题:(不良事件 OR 不良反应 OR 副作用 OR 毒性 OR 安全性)",
        "cnki": "SU=('镥177'+'氯化镥177'+'Lu-177'+'177Lu'+'无载体镥177'+'NCA Lu-177'+'lutenca'+'lunca')*('不良事件'+'不良反应'+'副作用'+'毒性'+'安全性')",
    },
}


def preset_query(drug: str, database: str) -> str:
    """返回 药物+数据库 对应的预设检索式；没有预设时返回空串。"""
    return DRUG_PRESETS.get(drug, {}).get(database, "")
