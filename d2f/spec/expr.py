"""Unit conversion and safe expression evaluation.

THE ONLY PLACE where mm <-> cm and deg <-> rad conversions happen.
Fusion's API uses cm for lengths and radians for angles internally;
part-spec.json uses mm and degrees.

This file is kept byte-identical in two places:
  addin/Drawing2Fusion/d2f_addin/expr.py   (runs inside Fusion)
  d2f/spec/expr.py                         (runs in the external CLI)
tests/unit/test_expr.py checks that they stay identical.
"""

import ast
import math
import operator
import re

MM_PER_CM = 10.0

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FUNCS = {"sqrt": math.sqrt, "abs": abs, "min": min, "max": max,
          "sin": lambda d: math.sin(math.radians(d)),
          "cos": lambda d: math.cos(math.radians(d)),
          "tan": lambda d: math.tan(math.radians(d))}
_CONSTS = {"pi": math.pi}
_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.Pow: operator.pow}
_UNOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def mm_to_cm(v):
    return float(v) / MM_PER_CM


def cm_to_mm(v):
    return float(v) * MM_PER_CM


def cm3_to_mm3(v):
    return float(v) * MM_PER_CM ** 3


def deg_to_rad(v):
    return math.radians(float(v))


def rad_to_deg(v):
    return math.degrees(float(v))


def identifiers(value):
    """Parameter names referenced by a value (number or expression string)."""
    if isinstance(value, (int, float)) or value is None:
        return set()
    return {n for n in _IDENT.findall(str(value)) if n not in _FUNCS and n not in _CONSTS}


def evaluate(value, params):
    """Evaluate a number or expression like 'L-10' or 'W/2' to a float (mm or deg).

    params: dict name -> number (already-resolved values).
    Raises KeyError for unknown names and ValueError for unsupported syntax.
    """
    if isinstance(value, bool):
        raise ValueError("boolean is not a dimension")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        raise ValueError("bad expression %r: %s" % (text, e))
    return float(_eval_node(tree.body, params, text))


def _eval_node(node, params, text):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in params:
            return float(params[node.id])
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise KeyError(node.id)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        return _BINOPS[type(node.op)](_eval_node(node.left, params, text),
                                      _eval_node(node.right, params, text))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNOPS:
        return _UNOPS[type(node.op)](_eval_node(node.operand, params, text))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in _FUNCS and not node.keywords:
        return _FUNCS[node.func.id](*[_eval_node(a, params, text) for a in node.args])
    raise ValueError("unsupported syntax in expression %r" % text)


def resolve_parameters(parameters):
    """Resolve the spec's `parameters` block into name -> float (mm/deg).

    Parameter values may reference earlier or later parameters; resolution
    iterates until stable. Raises ValueError on cycles / unknown names.
    """
    raw = {}
    for name, p in (parameters or {}).items():
        raw[name] = p.get("value") if isinstance(p, dict) else p
    resolved = {}
    remaining = dict(raw)
    while remaining:
        progressed = False
        for name in list(remaining):
            try:
                resolved[name] = evaluate(remaining[name], resolved)
            except KeyError:
                continue
            del remaining[name]
            progressed = True
        if not progressed:
            missing = {n: sorted(identifiers(v) - set(resolved)) for n, v in remaining.items()}
            raise ValueError("cannot resolve parameters (unknown or cyclic): %s" % missing)
    return resolved


# Names Fusion refuses as user parameters because they are unit symbols
# (A=ampere, L=liter, T=tesla, W=watt, ...). Measured on Fusion 2704.1.53 by
# trying every single letter; multi-letter unit names added by hand.
FUSION_RESERVED = set("ACEFHJKLNRSTVW" "cfglmrs") | {
    "mm", "cm", "km", "um", "nm", "in", "ft", "yd", "mi", "mil", "deg", "rad",
    "kg", "lb", "oz", "min", "hr", "sec", "Hz", "Pa", "psi", "pi"}


def fusion_name_map(names):
    """Map spec parameter names Fusion would reject to safe ones (L -> L_)."""
    names = list(names)
    taken = set(names)
    mapping = {}
    for n in names:
        if n in FUSION_RESERVED:
            new = n + "_"
            while new in taken or new in FUSION_RESERVED:
                new += "_"
            taken.add(new)
            mapping[n] = new
    return mapping


def rename_identifiers(value, mapping):
    """Apply fusion_name_map to an expression; numbers pass through."""
    if not mapping or isinstance(value, (int, float)) or value is None:
        return value
    return _IDENT.sub(lambda m: mapping.get(m.group(0), m.group(0)), str(value))


def to_fusion_expression(value, unit="mm"):
    """Expression string for adsk.core.ValueInput.createByString.

    Numbers get an explicit unit; strings are passed through (the design's
    default length unit is set to mm when the builder creates the document,
    so bare numbers inside expressions are mm).
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not unit:  # unitless (pattern counts): plain number, integer when possible
            return str(int(value)) if float(value).is_integer() else repr(float(value))
        return "%s %s" % (repr(float(value)), unit)
    return str(value)
