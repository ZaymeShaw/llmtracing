"""Non-detail intent contract shared by routing and local tool admission."""
from app.conf.rules import rules_conf


def unavailable_field_message(fields: list[str]) -> str | None:
    """Short circuit only exclusively unavailable fields; mixed requests keep usable IO."""
    special = rules_conf().fields.non_detail_fields
    aliases = {alias: code for code, names in special.items() for alias in [code, *names]}
    codes = [aliases.get(value) for value in fields]
    if not codes or any(code is None or code in {"waiting_period", "exclusion_clause"} for code in codes):
        return None
    names = list(dict.fromkeys(special[code][0] for code in codes if code is not None))
    return f"保单详情不包含{'、'.join(names)}，请通过保全或柜面渠道查询核实。"
