"""参数化场景（spec §6 params，里程碑 6）：声明参数 → 按种子确定性展开 → 抽样校验。

场景 JSON 可选字段 `params`：{参数名: 规格}，按声明顺序求值（规格里的字符串可以引用前面声明的参数）。
文本字段里用 `{参数名}` 占位，参数名只能是标识符（字母 / 数字 / 下划线，不以数字开头），
所以正则里的 `\\d{1,3}` 这类量词不受影响。

规格类型：
- int     {min, max, default, exclude?: [整数或 "{参数}"]}            区间内的整数
- choice  {values: [...], default}                                    候选列表之一
- ip      {net: "a.b.c.d/len", default, exclude?: [地址]}             网段内的主机地址（不含网络 / 广播地址）
- subnet  {within: "a.b.c.d/len", prefix, default, exclude?: [网段]}  大网段里的一个子网，值为网络地址
- host    {net: "{子网参数}/24", index}                               派生：网段的第 index 个地址（无随机）
- format  {value: "10.0.{x}.2"}                                       派生：按模板拼出的字符串（无随机）

种子：0 = 全部取 default（与参数化之前的原题等价，便于回归）；其他种子用
random.Random(f"{场景 id}#{种子}") 依次抽取，同一种子展开结果完全相同（可复现）。

抽样校验（scenarios.validate 调用）：种子 0、种子 1..SAMPLE_SEEDS、每个随机参数的边界值（最小 / 最大可用值，
choice 取全部候选），以及「碰撞」取值（其他参数的默认值、solution / checks 里出现的数字和 IP——参数恰好取到
正确值时故障不生效，随机抽样很难碰上）。每次展开都要通过场景的全部校验规则，且注入命令展开后不能与参考修复相同。

可以占位的字段：title、symptom、baseline / inject / restore / solution 的命令、checks 的 command / expect / desc、
hints、reference_path、scoring_points、note。expect 是正则：代入的值先 re.escape。
不能参数化：id、topology、设备名（required / accessible、各映射的设备键、checks.device）和接口名
（命令里 interface 后面的接口不能含占位符）——真实接口与设备由拓扑决定。
"""
from __future__ import annotations

import copy
import ipaddress
import random
import re
from typing import Any

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
TYPES = ("int", "choice", "ip", "subnet", "host", "format")
DERIVED = ("host", "format")
MAP_FIELDS = ("baseline", "inject", "restore", "solution")
TEXT_FIELDS = ("title", "symptom", "reference_path", "note")
# 抽样校验：固定种子 1..SAMPLE_SEEDS，加上种子 0（默认值）和各参数的边界值
SAMPLE_SEEDS = 24
_IFACE_RE = re.compile(r"(?i)\binterface\s+\S*\{")
# 进入视图的命令（同设备上注入与修复都要先进同一个视图，不算「注入值等于修复值」）
_VIEW_RE = re.compile(r"(?i)^(system-view|return|quit|interface\s+\S+|acl(\s+number)?\s+\S+|ospf(\s+\d+)?.*|area\s+\S+|"
                      r"bgp\s+\d+|isis(\s+\d+)?|rip(\s+\d+)?|vlan\s+\d+|aaa|user-interface\b.*)$")
_MAX_DRAW = 2000


class ParamError(ValueError):
    pass


def refs(text: Any) -> set[str]:
    return set(PLACEHOLDER.findall(text)) if isinstance(text, str) else set()


def has_params(s: dict[str, Any]) -> bool:
    return bool(s.get("params"))


# ---- 可占位的字符串（字段路径, 文本） ----

def _template_strings(s: dict[str, Any]):
    for f in TEXT_FIELDS:
        if isinstance(s.get(f), str):
            yield f, s[f]
    for f in MAP_FIELDS:
        m = s.get(f)
        if isinstance(m, dict):
            for dev, cmds in m.items():
                if isinstance(cmds, list):
                    for i, c in enumerate(cmds):
                        if isinstance(c, str):
                            yield f"{f}.{dev}[{i}]", c
    for i, c in enumerate(s.get("checks") or []):
        if isinstance(c, dict):
            for k in ("command", "expect", "desc"):
                if isinstance(c.get(k), str):
                    yield f"checks[{i}].{k}", c[k]
    for f in ("hints", "scoring_points"):
        v = s.get(f)
        if isinstance(v, list):
            for i, x in enumerate(v):
                if isinstance(x, str):
                    yield f"{f}[{i}]", x


def _fixed_strings(s: dict[str, Any]):
    """不能参数化的字段。"""
    for f in ("id", "topology"):
        if isinstance(s.get(f), str):
            yield f, s[f]
    for f in ("required_devices", "accessible_devices", "knowledge_tags"):
        for x in s.get(f) or []:
            if isinstance(x, str):
                yield f, x
    for f in MAP_FIELDS:
        m = s.get(f)
        if isinstance(m, dict):
            for dev in m:
                yield f"{f} 的设备名", dev
    for i, c in enumerate(s.get("checks") or []):
        if isinstance(c, dict) and isinstance(c.get("device"), str):
            yield f"checks[{i}].device", c["device"]


# ---- 规格校验 ----

def _spec_strings(spec: dict[str, Any]) -> list[str]:
    out = []
    for k in ("net", "within", "value"):
        if isinstance(spec.get(k), str):
            out.append(spec[k])
    for x in spec.get("exclude") or []:
        if isinstance(x, str):
            out.append(x)
    return out


def check_spec(s: dict[str, Any]) -> list[str]:
    """params 结构、引用关系、不可参数化字段的检查。返回错误列表（空 = 通过）。"""
    errs: list[str] = []
    params = s.get("params")
    if params is None:
        used = set()
        for path, text in _template_strings(s):
            used |= refs(text)
        if used:
            errs.append(f"引用了未声明的参数：{'、'.join(sorted(used))}（场景没有 params）")
        return errs
    if not isinstance(params, dict) or not params:
        return ["params 必须是非空对象 {参数名: 规格}"]
    declared: list[str] = []
    for name, spec in params.items():
        where = f"params.{name}"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            errs.append(f"{where}：参数名只能是标识符（字母、数字、下划线，不以数字开头）")
        if not isinstance(spec, dict) or spec.get("type") not in TYPES:
            errs.append(f"{where}：type 必须是 {' / '.join(TYPES)} 之一")
            declared.append(name)
            continue
        for r in set().union(*[refs(x) for x in _spec_strings(spec)] or [set()]):
            if r not in declared:
                errs.append(f"{where} 引用了{'自身' if r == name else '未声明或在其后声明'}的参数 {r}")
        t = spec["type"]
        if t == "int":
            mn, mx = spec.get("min"), spec.get("max")
            if not (isinstance(mn, int) and isinstance(mx, int)) or mn > mx:
                errs.append(f"{where}：int 需要整数 min ≤ max")
            elif not isinstance(spec.get("default"), int) or not mn <= spec["default"] <= mx:
                errs.append(f"{where}：default 必须是 [{mn}, {mx}] 内的整数")
        elif t == "choice":
            vals = spec.get("values")
            if not isinstance(vals, list) or not vals or not all(isinstance(v, (str, int)) for v in vals):
                errs.append(f"{where}：choice 需要非空 values（字符串或整数）")
            elif spec.get("default") not in vals:
                errs.append(f"{where}：default 必须是 values 之一")
        elif t == "ip":
            if not isinstance(spec.get("net"), str) or not isinstance(spec.get("default"), str):
                errs.append(f"{where}：ip 需要 net（如 10.0.12.0/24）与 default")
        elif t == "subnet":
            if not isinstance(spec.get("within"), str) or not isinstance(spec.get("prefix"), int) \
                    or not isinstance(spec.get("default"), str):
                errs.append(f"{where}：subnet 需要 within、prefix（整数）与 default（网络地址）")
        elif t == "host":
            if not isinstance(spec.get("net"), str) or not isinstance(spec.get("index"), int):
                errs.append(f"{where}：host 需要 net 与整数 index")
        elif t == "format":
            if not isinstance(spec.get("value"), str):
                errs.append(f"{where}：format 需要字符串 value")
        if "exclude" in spec and not isinstance(spec["exclude"], list):
            errs.append(f"{where}：exclude 必须是数组")
        declared.append(name)

    used: set[str] = set()
    for path, text in _template_strings(s):
        r = refs(text)
        for x in r - set(params):
            errs.append(f"{path} 引用了未声明的参数 {x}")
        used |= r
        if path.startswith(MAP_FIELDS) or path.endswith(".command"):
            if _IFACE_RE.search(text):
                errs.append(f"{path}：接口名不能参数化（{text!r}）")
    for spec in params.values():
        if isinstance(spec, dict):
            for x in _spec_strings(spec):
                used |= refs(x)
    for name in params:
        if name not in used:
            errs.append(f"参数 {name} 没有被任何字段引用")
    for path, text in _fixed_strings(s):
        if refs(text):
            errs.append(f"{path} 不能参数化（设备名 / 拓扑 / id 由真实拓扑决定）：{text!r}")
    return errs


# ---- 求值 ----

def _fill(text: str, values: dict[str, Any], escape: bool = False) -> str:
    def rep(m: re.Match) -> str:
        v = str(values[m.group(1)])
        return re.escape(v) if escape else v
    return PLACEHOLDER.sub(rep, text)


def _net(text: str, values: dict[str, Any]) -> ipaddress.IPv4Network:
    return ipaddress.IPv4Network(_fill(text, values), strict=False)


def _excluded(spec: dict[str, Any], values: dict[str, Any]) -> set[str]:
    out = {_fill(str(x), values) for x in spec.get("exclude") or []}
    if spec.get("type") == "subnet":          # 网段写法 10.0.10.0/24 与取值（网络地址）对齐
        out = {x.split("/")[0] for x in out}
    return out


def _candidates(name: str, spec: dict[str, Any], values: dict[str, Any]) -> tuple[int, Any]:
    """(候选个数, 取第 i 个的函数)。"""
    t = spec["type"]
    if t == "int":
        mn = spec["min"]
        return spec["max"] - mn + 1, lambda i: mn + i
    if t == "choice":
        vals = spec["values"]
        return len(vals), lambda i: vals[i]
    if t == "ip":
        net = _net(spec["net"], values)
        first = int(net.network_address) + (1 if net.prefixlen < 31 else 0)
        count = net.num_addresses - (2 if net.prefixlen < 31 else 0)
        return count, lambda i: str(ipaddress.IPv4Address(first + i))
    if t == "subnet":
        within = _net(spec["within"], values)
        p = spec["prefix"]
        if p < within.prefixlen or p > 32:
            raise ParamError(f"参数 {name}：prefix {p} 不在 {within} 之内")
        size = 1 << (32 - p)
        base = int(within.network_address)
        return 1 << (p - within.prefixlen), lambda i: str(ipaddress.IPv4Address(base + i * size))
    raise ParamError(f"参数 {name}：{t} 不是随机类型")


def _allowed(spec: dict[str, Any], v: Any, excl: set[str]) -> bool:
    return str(v) not in excl


def _derive(name: str, spec: dict[str, Any], values: dict[str, Any]) -> Any:
    if spec["type"] == "format":
        return _fill(spec["value"], values)
    net = _net(spec["net"], values)
    idx = spec["index"]
    if not 0 <= idx < net.num_addresses:
        raise ParamError(f"参数 {name}：index {idx} 超出 {net}")
    return str(net.network_address + idx)


def _check_value(name: str, spec: dict[str, Any], v: Any, values: dict[str, Any]) -> None:
    n, get = _candidates(name, spec, values)
    if spec["type"] == "int":
        ok = isinstance(v, int) and spec["min"] <= v <= spec["max"]
    elif spec["type"] == "choice":
        ok = v in spec["values"]
    elif spec["type"] == "ip":
        try:
            net = _net(spec["net"], values)
            a = ipaddress.IPv4Address(v)
            ok = a in net and (net.prefixlen >= 31 or a not in (net.network_address, net.broadcast_address))
        except ValueError:
            ok = False
    else:
        try:
            within = _net(spec["within"], values)
            sub = ipaddress.IPv4Network(f"{v}/{spec['prefix']}")
            ok = sub.subnet_of(within)
        except ValueError:
            ok = False
    if not ok:
        raise ParamError(f"参数 {name} 的取值 {v!r} 不合法")
    if not _allowed(spec, v, _excluded(spec, values)):
        raise ParamError(f"参数 {name} 的取值 {v!r} 在 exclude 中")


def _draw(name: str, spec: dict[str, Any], values: dict[str, Any], rng: random.Random) -> Any:
    n, get = _candidates(name, spec, values)
    excl = _excluded(spec, values)
    for _ in range(_MAX_DRAW):
        v = get(rng.randrange(n))
        if _allowed(spec, v, excl):
            return v
    allowed = [get(i) for i in range(min(n, 100000)) if _allowed(spec, get(i), excl)]
    if not allowed:
        raise ParamError(f"参数 {name} 排除后没有可选值")
    return allowed[rng.randrange(len(allowed))]


def values_for(s: dict[str, Any], seed: int, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """按种子求出全部参数值。种子 0 = 全部 default；overrides 强制某些参数的取值（边界抽样用），
    这时依赖它的参数若默认值不再合法（例如网段变了），改为确定性抽取。"""
    params = s.get("params") or {}
    rng = random.Random(f"{s.get('id', '')}#{int(seed)}")
    values: dict[str, Any] = {}
    for name, spec in params.items():
        t = spec["type"]
        if t in DERIVED:
            values[name] = _derive(name, spec, values)
            continue
        if overrides and name in overrides:
            v = overrides[name]
        elif seed == 0:
            v = spec["default"]
            if overrides:
                try:
                    _check_value(name, spec, v, values)
                except ParamError:
                    v = _draw(name, spec, values, rng)
        else:
            v = _draw(name, spec, values, rng)
        _check_value(name, spec, v, values)
        values[name] = v
    return values


def expand_raw(s: dict[str, Any], seed: int, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回代入参数后的场景副本（未做场景校验 / 规范化）。没有 params 时原样复制。

    展开结果不再带 params，另加 seed 与 param_values（记录本次取值）。
    """
    out = copy.deepcopy(s)
    if not has_params(s):
        out.pop("params", None)
        out["seed"] = int(seed)
        out["param_values"] = {}
        return out
    values = values_for(s, seed, overrides)
    for f in TEXT_FIELDS:
        if isinstance(out.get(f), str):
            out[f] = _fill(out[f], values)
    for f in MAP_FIELDS:
        if isinstance(out.get(f), dict):
            out[f] = {d: [_fill(c, values) if isinstance(c, str) else c for c in cmds] if isinstance(cmds, list)
                      else cmds for d, cmds in out[f].items()}
    for c in out.get("checks") or []:
        if isinstance(c, dict):
            for k in ("command", "desc"):
                if isinstance(c.get(k), str):
                    c[k] = _fill(c[k], values)
            if isinstance(c.get("expect"), str):
                c["expect"] = _fill(c["expect"], values, escape=True)
    for f in ("hints", "scoring_points"):
        if isinstance(out.get(f), list):
            out[f] = [_fill(x, values) if isinstance(x, str) else x for x in out[f]]
    out.pop("params", None)
    out["seed"] = int(seed)
    out["param_values"] = values
    return out


# ---- 抽样 ----

def boundary_overrides(s: dict[str, Any]) -> list[dict[str, Any]]:
    """边界值：每个随机参数取最小 / 最大的可用值（choice 取每个候选），其余参数取默认。"""
    params = s.get("params") or {}
    out: list[dict[str, Any]] = []
    for name, spec in params.items():
        if spec.get("type") in DERIVED:
            continue
        # 前面参数取默认值时的候选（边界只对本参数而言）
        base: dict[str, Any] = {}
        try:
            for n2, sp2 in params.items():
                if n2 == name:
                    break
                base[n2] = _derive(n2, sp2, base) if sp2["type"] in DERIVED else sp2["default"]
            n, get = _candidates(name, spec, base)
            excl = _excluded(spec, base)
        except (ParamError, ValueError, KeyError):
            continue
        if spec["type"] == "choice":
            picks = [get(i) for i in range(n)]
        else:
            lo = next((get(i) for i in range(min(n, 4096)) if _allowed(spec, get(i), excl)), None)
            hi = next((get(i) for i in range(n - 1, max(-1, n - 4097), -1) if _allowed(spec, get(i), excl)), None)
            picks = [x for x in (lo, hi) if x is not None]
        for v in picks:
            if v != spec.get("default") and {name: v} not in out:
                out.append({name: v})
    return out


def collision_overrides(s: dict[str, Any]) -> list[dict[str, Any]]:
    """碰撞抽样：参数取到「正确值」时故障就不生效，随机抽样很难恰好碰上，所以专门试一遍。

    正确值的来源：其他同类参数的默认值，以及种子 0 展开后 solution / checks 里出现的数字和 IP
    （例如 port default vlan 10 里的 10、ip address 10.0.12.2 24 里的 12）。只试本参数允许的取值。
    """
    params = s.get("params") or {}
    try:
        e0 = expand_raw(s, 0)
    except (ParamError, ValueError, KeyError):
        return []
    text = " ".join(c for cmds in (e0.get("solution") or {}).values() for c in cmds if isinstance(c, str))
    text += " " + " ".join(str(c.get("command", "")) for c in e0.get("checks") or [] if isinstance(c, dict))
    ints = {int(x) for x in re.findall(r"\d+", text) if len(x) <= 6}
    ips = set(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text))
    base: dict[str, Any] = {}
    out: list[dict[str, Any]] = []
    for name, spec in params.items():
        t = spec.get("type")
        if t in DERIVED:
            try:
                base[name] = _derive(name, spec, base)
            except (ParamError, ValueError, KeyError):
                return out
            continue
        cands: set[Any] = set()
        if t == "int":
            cands = set(ints) | {p["default"] for n2, p in params.items() if n2 != name and p.get("type") == "int"}
        elif t == "ip":
            cands = set(ips) | {p["default"] for n2, p in params.items() if n2 != name and p.get("type") == "ip"}
        for v in sorted(cands, key=str):
            if v == spec.get("default"):
                continue
            try:
                _check_value(name, spec, v, base)
            except (ParamError, ValueError):
                continue
            out.append({name: v})
        base[name] = spec.get("default")
    return out


def samples(s: dict[str, Any], n: int = SAMPLE_SEEDS) -> list[tuple[str, int, dict[str, Any] | None]]:
    """抽样计划：[(标签, 种子, overrides)]。"""
    plan: list[tuple[str, int, dict[str, Any] | None]] = [("种子 0（默认值）", 0, None)]
    plan += [(f"种子 {i}", i, None) for i in range(1, n + 1)]
    for ov in boundary_overrides(s):
        k, v = next(iter(ov.items()))
        plan.append((f"边界 {k}={v}", 0, ov))
    bounds = [ov for _, _, ov in plan if ov]
    for ov in collision_overrides(s):
        if ov not in bounds:
            k, v = next(iter(ov.items()))
            plan.append((f"碰撞 {k}={v}", 0, ov))
    return plan


def injected_equals_fix(template: dict[str, Any], expanded: dict[str, Any]) -> list[str]:
    """inject 中含参数的命令展开后与同设备 solution 的某条命令相同 → 故障在这次取值下可能不生效。"""
    errs = []
    t_inj = template.get("inject") or {}
    e_inj = expanded.get("inject") or {}
    sol = expanded.get("solution") or {}
    for dev, cmds in t_inj.items():
        if not isinstance(cmds, list):
            continue
        fixes = {re.sub(r"\s+", " ", c.strip().lower()) for c in sol.get(dev, []) if isinstance(c, str)}
        for i, c in enumerate(cmds):
            if isinstance(c, str) and refs(c) and not _VIEW_RE.match(c.strip()):
                e = e_inj.get(dev, [])[i] if i < len(e_inj.get(dev, [])) else ""
                if re.sub(r"\s+", " ", e.strip().lower()) in fixes:
                    errs.append(f"inject.{dev} 的 {e!r} 与参考修复命令相同（故障不会生效，检查 exclude）")
    return errs
