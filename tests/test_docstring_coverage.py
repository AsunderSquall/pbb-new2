"""注释覆盖率的可执行基线。

这个测试不检查代码行为，而是在守护一条工程约束：**核心模块的公开函数/类必须带
docstring，且注释中要出现中文**。项目约定所有说明性注释用中文（见 REPO.md），
所以"有 docstring"在这里同时意味着"有可用中文文档"。

为什么需要这个测试：注释一旦没有约束就会随重构持续流失。这里给出可执行的基线，
任何一次改动如果让覆盖率或中文覆盖数掉到基线以下，测试立刻失败，改动者必须补回。

数值基线是实测结果 + 少量余量，只允许上调不允许下调。
"""
from pathlib import Path
import ast
import sys
import unittest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / 'tools'))

ROOT = Path(__file__).resolve().parent.parent

# 受约束的核心模块：pbb/ 包 + tools/ 下的构建与打包脚本 + 新增的 paths 路径模块。
# 测试自身不在范围内（测试的注释要求由 tests/ 下的文件各自承担）。
CORE_MODULES = (
    "pbb/main.py", "pbb/engine.py", "pbb/ui.py", "pbb/paths.py",
    "pbb/build_settings.py", "pbb/config_schema.py", "pbb/autotune.py", "pbb/_build.py",
    "tools/package_release.py", "tools/package_offline.py", "tools/rebuild_local.py",
)

# 实测基线（重构为 pbb/ 包结构后重新测定）：66 个公开定义中 36 个带 docstring，
# 其中 35 条含中文。基线只允许**上调**：后续补了更多注释就把这两个数字一起抬高。
MIN_DOCUMENTED_PUBLIC = 36
# 其中至少这么多条 docstring 必须含中文（项目约定注释用中文）。
MIN_CHINESE_DOCSTRINGS = 35


def _has_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _public_defs(tree: ast.AST):
    """产出 (限定名, 节点) —— 顶层函数、顶层类，以及类里的公开方法。"""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                yield node.name, node
        elif isinstance(node, ast.ClassDef):
            # 类本身也算，公开与否都统计（它们是模块的主要入口）。
            yield node.name, node
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # 仅公开方法；私有助手与嵌套函数不计入。
                    if not child.name.startswith("_"):
                        yield f"{node.name}.{child.name}", child


class DocstringCoverageTests(unittest.TestCase):
    def collect(self):
        """扫描核心模块，返回 (公开定义总数, 有 docstring 数, 含中文 docstring 数)。"""
        total = documented = chinese = 0
        for rel in CORE_MODULES:
            path = ROOT / rel
            self.assertTrue(path.is_file(), f"核心模块缺失: {rel}")
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for _name, node in _public_defs(tree):
                total += 1
                doc = ast.get_docstring(node)
                if doc and doc.strip():
                    documented += 1
                    if _has_chinese(doc):
                        chinese += 1
        return total, documented, chinese

    def test_public_definitions_keep_docstrings(self):
        total, documented, chinese = self.collect()
        self.assertGreaterEqual(
            documented, MIN_DOCUMENTED_PUBLIC,
            f"带 docstring 的公开定义只有 {documented} 个（共 {total} 个），"
            f"低于基线 {MIN_DOCUMENTED_PUBLIC}。新增公开函数/类时请补中文 docstring。")

    def test_docstrings_are_written_in_chinese(self):
        _total, documented, chinese = self.collect()
        self.assertGreaterEqual(
            chinese, MIN_CHINESE_DOCSTRINGS,
            f"含中文的 docstring 只有 {chinese} 条（共 {documented} 条带 docstring），"
            f"低于基线 {MIN_CHINESE_DOCSTRINGS}。项目约定说明性注释用中文。")


if __name__ == "__main__":
    unittest.main()
