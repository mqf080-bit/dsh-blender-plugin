#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态分析 runtime/*.py 里的 bpy.context.view_layer.update() 调用点：
按文件 / 函数聚合，并标出是否在循环体内（loop_depth>0）。输出 JSON，便于做「减少中间 update」的改造清单。
"""
import ast, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[2] / "runtime"


def is_vl_update(node):
    # bpy.context.view_layer.update()
    f = node.func
    if not isinstance(f, ast.Attribute) or f.attr != "update":
        return False
    v = f.value
    return (isinstance(v, ast.Attribute) and v.attr == "view_layer"
            and isinstance(v.value, ast.Attribute) and v.value.attr == "context"
            and isinstance(v.value.value, ast.Name) and v.value.value.id == "bpy")


class V(ast.NodeVisitor):
    def __init__(self, path):
        self.path = path
        self.stack = [("<module>", 0)]   # (func, loop_depth)
        self.rows = []
        self.func_calls = {}

    def visit_FunctionDef(self, node):
        self.stack.append((node.name, 0))
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_For(self, node):
        self._loop(node)
    def visit_While(self, node):
        self._loop(node)
    def visit_AsyncFor(self, node):
        self._loop(node)

    def _loop(self, node):
        fn, depth = self.stack[-1]
        self.stack[-1] = (fn, depth + 1)
        self.generic_visit(node)
        fn, depth = self.stack[-1]
        self.stack[-1] = (fn, depth - 1)

    def visit_Call(self, node):
        if is_vl_update(node):
            fn, depth = self.stack[-1]
            self.rows.append({"file": self.path.name, "line": node.lineno, "function": fn,
                              "loop_depth": depth})
            self.func_calls[fn] = self.func_calls.get(fn, 0) + 1
        self.generic_visit(node)


def main():
    files = sorted(ROOT.glob("*.py"))
    rows = []
    for p in files:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception as e:
            print("SKIP %s: %r" % (p.name, e), file=sys.stderr)
            continue
        v = V(p); v.visit(tree); rows.extend(v.rows)
    rows.sort(key=lambda r: (r["file"], r["line"]))
    out = {"root": str(ROOT), "total": len(rows),
           "in_loops": sum(1 for r in rows if r["loop_depth"] > 0),
           "files": {}} 
    for r in rows:
        f = out["files"].setdefault(r["file"], {"calls": 0, "in_loops": 0, "functions": {}})
        f["calls"] += 1
        if r["loop_depth"] > 0:
            f["in_loops"] += 1
        f["functions"][r["function"]] = f["functions"].get(r["function"], 0) + 1
    print(json.dumps({"summary": {"total": out["total"], "in_loops": out["in_loops"],
                                  "files": {k: {"calls": v["calls"], "in_loops": v["in_loops"]}
                                            for k, v in out["files"].items()}},
                      "rows": rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
