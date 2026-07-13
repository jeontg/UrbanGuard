"""SemanticRiskAgent — crowd threat inference (weighted score over SAM3 +
behavior signals). Pure logic, no GPU dependency.

Ported from SAM's ``SAM3_install.py`` (Stage 4). Structurally the crowd-domain
counterpart of ``traffic_weather.agents.semantic_agent.SemanticAgent`` (both
fuse quantitative signals + VLM text into a graded risk code) — kept separate
because the taxonomies and weighting are domain-specific.
"""
from __future__ import annotations

import numpy as np

RISK_TAXONOMY = {
    "NORMAL": ("정상", 0), "CROWD_DENSITY_HIGH": ("군중밀집", 1),
    "FLOW_CHAOS": ("이동흐름혼란", 2), "CROWD_SURGE_RISK": ("군중급증위험", 3),
    "PANIC_DISPERSION": ("패닉분산", 4),
}


class SemanticRiskAgent:
    def __init__(self, density_hi=40.0, surge_hi=1.6, disp_hi=0.6, diverg_hi=6.0):
        self.density_hi = density_hi
        self.surge_hi = surge_hi
        self.disp_hi = disp_hi
        self.diverg_hi = diverg_hi

    def assess(self, sam3_summary: dict, behavior: dict, vlm_text: str) -> dict:
        d = sam3_summary["density_pct"]
        sg = behavior["surge"]
        dp = behavior["dispersion"]
        dv = behavior["divergence"]
        kw = vlm_text
        s_density = min(d / 60.0, 1.0)
        s_surge = min(max(sg - 1.0, 0) / 1.0, 1.0)
        s_disp = min(dp / 1.0, 1.0)
        s_diverg = min(max(dv, 0) / 12.0, 1.0)
        score = float(np.clip(0.35 * s_density + 0.25 * s_surge +
                              0.2 * s_disp + 0.2 * s_diverg, 0, 1))
        drivers = []
        code = "NORMAL"
        if (dv > self.diverg_hi or "분산" in kw or "패닉" in kw) and sg > 1.3:
            code = "PANIC_DISPERSION"
            drivers += ["발산도UP", "속도급증"]
        elif sg > self.surge_hi or "급증" in kw:
            code = "CROWD_SURGE_RISK"
            drivers += ["속도급증비UP"]
        elif dp > self.disp_hi or "혼란" in kw or "역류" in kw or "병목" in kw:
            code = "FLOW_CHAOS"
            drivers += ["방향분산도UP"]
        elif d > self.density_hi or "혼잡" in kw:
            code = "CROWD_DENSITY_HIGH"
            drivers += ["밀집도UP"]
        if d > self.density_hi and "밀집" not in " ".join(drivers):
            drivers.append("밀집도UP")
        name_kr, sev = RISK_TAXONOMY[code]
        return dict(risk_code=code, risk_name_kr=name_kr, severity=sev,
                    score=round(score, 3), context=kw, drivers=drivers)
