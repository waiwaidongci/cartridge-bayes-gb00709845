"""Classify tested printer cartridges using a training-only Gaussian model."""

from datetime import date, timedelta
from math import exp, fsum, log
from random import Random
from statistics import mean, pvariance


RETURN = "退件"
KEEP = "留用"
PENDING = "待判"
TRAIN = "训练"
HOLDOUT = "留出"
CANDIDATE = "候选"
FEATURES = (
    ("remaining_pct", "剩余耗材率"),
    ("streak_pct", "条纹覆盖率"),
    ("density_error_pct", "密度偏差率"),
)
VARIANCE_FLOOR = 0.01
DEFAULT_FALSE_RETURN_COST = 35
DEFAULT_MISSED_RETURN_COST = 115
COST_MIN = 0.01
COST_MAX = 10000
SAMPLE_SEED = 702411


def sample_rows() -> list[dict]:
    rng = Random(SAMPLE_SEED)
    schools = ("澄江一小", "澄江实验中学", "澄江职校")
    rows = []
    historical = (
        (TRAIN, date(2026, 11, 2), KEEP, 56, (28, 9, 14)),
        (TRAIN, date(2026, 11, 2), RETURN, 24, (46, 19, 24)),
        (HOLDOUT, date(2026, 11, 16), KEEP, 25, (30, 10, 15)),
        (HOLDOUT, date(2026, 11, 16), RETURN, 15, (44, 17, 22)),
    )
    deviations = (15, 6, 8)
    for cohort, start, label, count, centers in historical:
        for offset in range(count):
            values = tuple(
                round(min(100, max(0, rng.gauss(center, spread))), 2)
                for center, spread in zip(centers, deviations)
            )
            rows.append(_sample_row(
                len(rows) + 1, schools[rng.randrange(len(schools))],
                start + timedelta(days=offset % 5), cohort, label, values,
            ))

    candidate_values = (
        (51, 24, 31), (13, 4, 11), (37, 16, 21), (82, 8, 17),
        (41, 13, 18), (19, 22, 29), (34, 17, 21), (29, 12, 17),
        (47, 11, 17), (31, 13, 23), (52, 19, 23), (26, 8, 16),
    )
    for offset, values in enumerate(candidate_values):
        rows.append(_sample_row(
            len(rows) + 1, schools[offset % len(schools)], date(2026, 11, 23),
            CANDIDATE, PENDING, values,
        ))
    return rows


def _sample_row(number, school, inspected_on, cohort, label, values):
    return {
        "cartridge_id": f"CJ-2611-{number:03}",
        "school": school,
        "inspected_on": inspected_on.isoformat(),
        "cohort": cohort,
        "verified_label": label,
        **{key: value for (key, _), value in zip(FEATURES, values)},
    }


def _group_rows(rows):
    groups = {TRAIN: [], HOLDOUT: [], CANDIDATE: []}
    seen_ids = set()
    for number, row in enumerate(rows, 2):
        cartridge = row.get("cartridge_id")
        if not isinstance(cartridge, str) or not cartridge.strip():
            raise ValueError(f"CSV 第 {number} 行缺少耗材编号，请填写实物唯一编号。")
        cartridge = cartridge.strip()
        prefix = f"CSV 第 {number} 行（{cartridge}）"
        if cartridge in seen_ids:
            raise ValueError(f"{prefix}耗材编号重复，请合并重测记录并确保同一耗材只出现一次。")
        seen_ids.add(cartridge)
        school = row.get("school")
        if not isinstance(school, str) or not school.strip():
            raise ValueError(f"{prefix}缺少校点，请填写送检学校名称。")
        inspected_on = row.get("inspected_on")
        try:
            if date.fromisoformat(inspected_on).isoformat() != inspected_on:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"{prefix}检测日期无效，请按 YYYY-MM-DD 填写。") from None
        cohort = row.get("cohort")
        if cohort not in (TRAIN, HOLDOUT, CANDIDATE):
            raise ValueError(f"{prefix}分组无效，请填写训练、留出或候选。")
        label = row.get("verified_label")
        allowed_labels = (PENDING,) if cohort == CANDIDATE else (RETURN, KEEP)
        if label not in allowed_labels:
            allowed = "或".join(allowed_labels)
            raise ValueError(f"{prefix}{cohort}组结论无效，请填写{allowed}。")
        for key, feature in FEATURES:
            value = row.get(key)
            if type(value) not in (int, float) or not 0 <= value <= 100:
                raise ValueError(f"{prefix}{feature}无效，请填写 0 至 100 的百分数。")
        groups[cohort].append({**row, "cartridge_id": cartridge, "school": school.strip()})

    for cohort, minimum in ((TRAIN, 2), (HOLDOUT, 1)):
        for label in (RETURN, KEEP):
            count = sum(row["verified_label"] == label for row in groups[cohort])
            if count < minimum:
                raise ValueError(f"{cohort}组至少需要 {minimum} 条已确认{label}的记录，请补齐后重跑。")
    if not groups[CANDIDATE]:
        raise ValueError("候选组为空，请加入至少 1 条结论为待判的送检记录。")
    for group in groups.values():
        group.sort(key=lambda row: row["cartridge_id"])
    return groups


def _cost(options, key, label, default):
    value = options.get(key, default)
    if type(value) not in (int, float) or not COST_MIN <= value <= COST_MAX:
        raise ValueError(f"{label}无效，请输入 {COST_MIN} 至 {COST_MAX} 元的有限金额。")
    return float(value)


def _fit(training):
    by_label = {label: [row for row in training if row["verified_label"] == label]
                for label in (RETURN, KEEP)}
    prior_log_odds = log(len(by_label[RETURN]) / len(by_label[KEEP]))
    distributions = {}
    for label, members in by_label.items():
        distributions[label] = []
        for key, _ in FEATURES:
            values = [row[key] for row in members]
            distributions[label].append((mean(values), max(pvariance(values), VARIANCE_FLOOR)))
    return prior_log_odds, distributions[RETURN], distributions[KEEP]


def _score(row, model):
    prior_log_odds, return_distributions, keep_distributions = model
    contributions = []
    for (key, label), (return_mean, return_var), (keep_mean, keep_var) in zip(
        FEATURES, return_distributions, keep_distributions,
    ):
        value = row[key]
        log_ratio = -0.5 * (
            log(return_var / keep_var)
            + (value - return_mean) ** 2 / return_var
            - (value - keep_mean) ** 2 / keep_var
        )
        contributions.append((label, value, log_ratio))
    log_odds = fsum([prior_log_odds, *(term for _, _, term in contributions)])
    tail = exp(-abs(log_odds))
    probability = 1 / (1 + tail) if log_odds >= 0 else tail / (1 + tail)
    contributions.sort(key=lambda feature: -abs(feature[2]))
    evidence = "；".join(
        f"{label} {value:g}%（{round(term, 6) or 0.0:+.6f}）"
        for label, value, term in contributions
    )
    return probability, log_odds, evidence


def _decision(log_odds, cutoff):
    return RETURN if log_odds >= cutoff else KEEP


def _classification_counts(results, action_key):
    counts = {(actual, predicted): 0 for actual in (RETURN, KEEP) for predicted in (RETURN, KEEP)}
    for result in results:
        counts[result["质检结论"], result[action_key]] += 1
    return counts[RETURN, RETURN], counts[KEEP, KEEP], counts[KEEP, RETURN], counts[RETURN, KEEP]


def _metrics(prefix, counts, false_return_cost, missed_return_cost):
    true_return, true_keep, false_return, missed_return = counts
    total = sum(counts)
    recall = true_return / (true_return + missed_return)
    return {
        f"{prefix}正确退件数": true_return,
        f"{prefix}正确留用数": true_keep,
        f"{prefix}误退数": false_return,
        f"{prefix}漏退数": missed_return,
        f"{prefix}准确率": round((true_return + true_keep) / total, 6),
        f"{prefix}退件召回率": round(recall, 6),
        f"{prefix}错判成本（元）": round(false_return * false_return_cost + missed_return * missed_return_cost, 4),
    }


def compute(rows: list[dict], options: dict) -> dict:
    false_return_cost = _cost(options, "false_return_cost", "误退单件成本", DEFAULT_FALSE_RETURN_COST)
    missed_return_cost = _cost(options, "missed_return_cost", "漏退单件成本", DEFAULT_MISSED_RETURN_COST)
    threshold = false_return_cost / (false_return_cost + missed_return_cost)
    cutoff = log(false_return_cost / missed_return_cost)
    groups = _group_rows(rows)
    model = _fit(groups[TRAIN])
    results = {HOLDOUT: [], CANDIDATE: []}
    for cohort, details in results.items():
        scored = [(row, _score(row, model)) for row in groups[cohort]]
        if cohort == CANDIDATE:
            scored.sort(key=lambda item: -item[1][1])
        for row, (probability, log_odds, evidence) in scored:
            details.append({
                "耗材编号": row["cartridge_id"],
                "校点": row["school"],
                "检测日期": row["inspected_on"],
                "分组": cohort,
                "质检结论": row["verified_label"],
                "退件后验概率": round(probability, 8),
                "成本阈值判定": _decision(log_odds, cutoff),
                "0.5 阈值判定": _decision(log_odds, 0),
                "退件期望损失（元）": round((1 - probability) * false_return_cost, 4),
                "留用期望损失（元）": round(probability * missed_return_cost, 4),
                "先验对数赔率": round(model[0], 6),
                "后验对数赔率": round(log_odds, 6),
                "检测值与对数赔率贡献": evidence,
            })

    holdout = results[HOLDOUT]
    candidates = results[CANDIDATE]
    candidate_returns = sum(row["成本阈值判定"] == RETURN for row in candidates)
    cost_metrics = _metrics(
        "成本阈值", _classification_counts(holdout, "成本阈值判定"), false_return_cost, missed_return_cost,
    )
    reference_metrics = _metrics(
        "0.5 阈值", _classification_counts(holdout, "0.5 阈值判定"), false_return_cost, missed_return_cost,
    )
    cost_loss = cost_metrics["成本阈值错判成本（元）"]
    reference_loss = reference_metrics["0.5 阈值错判成本（元）"]
    return {
        "headline": (
            f"{len(candidates)} 件候选中建议退件 {candidate_returns} 件，"
            f"{len(holdout)} 件留出实测错判成本为 {cost_loss:g} 元，0.5 阈值为 {reference_loss:g} 元。"
        ),
        "metrics": {
            "训练件数": len(groups[TRAIN]),
            "训练已确认退件数": sum(row["verified_label"] == RETURN for row in groups[TRAIN]),
            "留出件数": len(holdout),
            "候选件数": len(candidates),
            "候选建议退件数": candidate_returns,
            "误退单件成本（元）": false_return_cost,
            "漏退单件成本（元）": missed_return_cost,
            "成本退件阈值": round(threshold, 8),
            **cost_metrics,
            **reference_metrics,
            "较 0.5 阈值节省（元）": round(reference_loss - cost_loss, 4),
        },
        "rows": candidates + holdout,
        "notes": [
            "按 CSV 固定分组。每件耗材有唯一实物编号。先验、特征均值和总体方差仅从训练组估计，留出组参与对照计分，候选组用于待判件复核。",
            f"高斯朴素贝叶斯假定给定质检结论时各检测特征独立，特征方差下限为 {VARIANCE_FLOOR:g} 平方百分点。后验为模型估计。",
            "成本阈值等于误退单件成本 ÷（误退单件成本 + 漏退单件成本）。两种期望损失相等时建议退件。留出错判成本按实测误退和漏退件数计费。",
            "候选按退件后验由高到低排列，同分按耗材编号排列。贡献为该检测值的对数似然比，按绝对值降序展示。正值支持退件，负值支持留用，与先验对数赔率相加得到后验对数赔率。",
            "对照使用同一份留出台账与同一组后验。成本取自复核前确认的业务估价，修改成本后请保留原评估报告以便复盘。",
        ],
        "chart": {
            "label": "同一留出组的错判成本",
            "unit": "元",
            "points": [
                {"label": "成本阈值", "value": cost_loss},
                {"label": "0.5 阈值", "value": reference_loss},
            ],
        },
        "can_publish": True,
    }
