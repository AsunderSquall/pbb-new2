"""把 README.md / 先读我.md 导出成发布包用的 PDF。

**为什么不直接 `pandoc x.md -o x.pdf`**：pandoc 自己不会排版 PDF，必须借助引擎；
本机没有 xelatex/lualatex/tectonic，`pandoc --pdf-engine=xelatex` 会直接失败。
所以走「pandoc → 单文件 HTML → Chromium 无头打印」这条路：Chromium（Chrome/Edge）
在每台 Windows 上都有，中文字体用系统自带的微软雅黑，表格/代码块的分页与边距由 CSS 控制。

用法（仓库根）：
    python tools/make_pdfs.py            # 重新生成 README.pdf 与 先读我.pdf
    python tools/make_pdfs.py --check    # 只检查工具链与输入，不生成
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
# 与 tools/package_release.PDF_SOURCES 保持一致：Markdown 是版本控制的真源，PDF 是导出物。
PDF_SOURCES = (("README.md", "README.pdf"), ("先读我.md", "先读我.pdf"))

# Chromium 内核浏览器的常见位置（Chrome 优先：对 --no-pdf-header-footer 支持更稳）。
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

# 打印样式：A4、微软雅黑、表格细边框、代码块不跨页、标题不孤行。
STYLE = """
@page { size: A4; margin: 18mm 16mm; }
body { font-family: "Microsoft YaHei", "Sarasa Mono SC", sans-serif; font-size: 10.5pt;
       line-height: 1.6; color: #1a1a1a; }
h1 { font-size: 20pt; border-bottom: 2px solid #333; padding-bottom: 6px; }
h2 { font-size: 15pt; margin-top: 18px; border-bottom: 1px solid #bbb; padding-bottom: 4px; }
h3 { font-size: 12.5pt; margin-top: 14px; }
table { border-collapse: collapse; width: 100%; font-size: 9.5pt; }
th, td { border: 1px solid #999; padding: 4px 6px; vertical-align: top; }
th { background: #f0f0f0; }
pre { background: #f6f6f6; border: 1px solid #ddd; padding: 8px; font-size: 8.5pt;
      white-space: pre-wrap; word-break: break-all; page-break-inside: avoid; }
code { font-family: "Cascadia Mono", Consolas, monospace; font-size: 9pt; }
blockquote { border-left: 3px solid #bbb; margin-left: 0; padding-left: 10px; color: #444; }
h1, h2, h3 { page-break-after: avoid; }
"""


def find_chrome() -> str:
    """找到 Chromium 内核浏览器；找不到就报错（pandoc 单独无法出 PDF）。"""
    for candidate in CHROME_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    found = shutil.which("chrome") or shutil.which("msedge")
    if found:
        return found
    raise SystemExit("找不到 Chrome/Edge：pandoc 需要借助浏览器或 LaTeX 引擎才能输出 PDF。\n"
                     "安装 Chrome/Edge，或用 --pdf-engine=xelatex 自行导出。")


def find_pandoc() -> str:
    """pandoc 是 Markdown → HTML 的唯一转换器。"""
    found = shutil.which("pandoc")
    if not found:
        raise SystemExit("找不到 pandoc：请先安装 https://pandoc.org/")
    return found


def build_html(pandoc: str, md: Path, html: Path) -> None:
    """用 pandoc 把 Markdown 转成单文件 HTML（带打印样式）。"""
    subprocess.run(
        [pandoc, str(md), "-f", "gfm+hard_line_breaks", "-t", "html5", "--standalone",
         "--metadata", f"title={md.stem}", "--css", "pbb-print.css",
         "-o", str(html)],
        check=True, cwd=str(md.parent))
    # pandoc 的 --css 只是引用；把样式内联进去，Chromium 打开单文件即可。
    style = md.parent / "pbb-print.css"
    style.write_text(STYLE, encoding="utf8")
    try:
        content = html.read_text(encoding="utf8")
        content = content.replace('<link rel="stylesheet" href="pbb-print.css" />',
                                  f"<style>{STYLE}</style>")
        html.write_text(content, encoding="utf8")
    finally:
        style.unlink(missing_ok=True)


def build_pdf(chrome: str, html: Path, pdf: Path) -> None:
    """Chromium 无头打印：A4、无页眉页脚、背景色保留。"""
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--user-data-dir={profile}", "--no-pdf-header-footer", "--print-to-pdf-no-header",
             "--no-first-run", "--no-default-browser-check",
             f"--print-to-pdf={pdf}", html.as_uri()],
            check=True, capture_output=True)
    if not pdf.is_file() or pdf.stat().st_size < 4096:
        raise SystemExit(f"PDF 未生成或过小：{pdf}")
    with open(pdf, "rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise SystemExit(f"输出不是 PDF：{pdf}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只检查工具链与输入")
    args = parser.parse_args()
    pandoc = find_pandoc()
    chrome = find_chrome()
    missing = [md for md, _ in PDF_SOURCES if not (ROOT / md).is_file()]
    if missing:
        raise SystemExit("缺少 Markdown 源文件: " + ", ".join(missing))
    print(f"pandoc : {pandoc}")
    print(f"chrome : {chrome}")
    if args.check:
        return 0
    with tempfile.TemporaryDirectory() as temp:
        for md_name, pdf_name in PDF_SOURCES:
            md = ROOT / md_name
            html = Path(temp) / (Path(md_name).stem + ".html")
            pdf = ROOT / pdf_name
            print(f"[pdf] {md_name} -> {pdf_name}", flush=True)
            build_html(pandoc, md, html)
            build_pdf(chrome, html, pdf)
            print(f"       {pdf.stat().st_size / 1024:.0f} KB", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
