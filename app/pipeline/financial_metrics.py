"""Small deterministic comparisons for research answers, with explicit periods."""


def _number(value):
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def _growth(current, previous):
    return round((current - previous) / abs(previous) * 100, 2) if current is not None and previous else None


def financial_metrics(snapshot: dict) -> dict:
    quarters = snapshot.get("quarters", {})
    periods = list(quarters)
    comparisons = {}
    for index, period in enumerate(periods):
        comparisons[period] = {}
        for metric, raw in quarters[period].items():
            current = _number(raw)
            previous = _number(quarters[periods[index - 1]].get(metric)) if index else None
            year_ago = _number(quarters[periods[index - 4]].get(metric)) if index >= 4 else None
            comparisons[period][metric] = {"value": current, "qoq_pct": _growth(current, previous),
                                            "yoy_pct": _growth(current, year_ago)}
    cash_flow = snapshot.get("cash_flow", {})
    profit_loss = snapshot.get("profit_loss", {})
    cfo = next((row for label, row in cash_flow.items() if "operating" in label.casefold()), {})
    pat = next((row for label, row in profit_loss.items() if label.casefold() == "net profit"), {})
    cash_conversion = {}
    for period, raw in cfo.items():
        operating_cash, profit = _number(raw), _number(pat.get(period))
        cash_conversion[period] = {"cfo": operating_cash, "pat": profit,
                                  "cfo_to_pat": round(operating_cash / profit, 3) if operating_cash is not None and profit else None}
    return {"quarterly_comparisons": comparisons, "cash_conversion": cash_conversion}
