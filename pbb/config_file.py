"""测号配置的读写：按文件扩展名在 YAML / JSON / TOML 之间原样保留格式。

**为什么单独一个模块**：界面和命令行都要写配置（「自动更新 start」、编辑器保存、
`main.py` 的自动更新 start），而 ``pbb/ui.py`` 会连带拉起 Tk —— 命令行里 import 它
既浪费又可能在没有显示环境时直接失败。这里只依赖标准库（与可选的 PyYAML），
两边共用同一套"保留原格式 + 原子替换"的写盘逻辑，避免两份实现漂移。
"""
import json
import os
from pathlib import Path


def load_config(path, base_dir=None) -> dict:
    """按扩展名读取 YAML / JSON / TOML 配置。

    ``base_dir`` 用于把相对路径解析到配置目录（界面里选中的往往只是一个裸文件名）；
    传 None 表示按当前工作目录解析。读完后统一走 ``flex.resolve_files``，把字符集
    文件里的相对路径按配置文件所在目录补全。
    """
    path = os.fspath(path)
    if base_dir is not None and not os.path.isabs(path):
        path = os.path.join(os.fspath(base_dir), path)
    suffix = os.path.splitext(path)[1].lower()
    if suffix == '.toml':
        import tomllib
        with open(path, 'rb') as handle:
            cfg = tomllib.load(handle)
    else:
        with open(path, 'r', encoding='utf-8') as handle:
            cfg = json.load(handle) if suffix == '.json' else __import__('yaml').safe_load(handle)
    from .flex import resolve_files
    return resolve_files(cfg, path)


def save_config(path, cfg) -> None:
    """保存配置到指定文件，保留该文件原有的格式（YAML / JSON / TOML）。

    TOML 没有标准库写入器，这里手写最小序列化器：只支持配置里实际用到的标量、列表与
    单层内联表；遇到其它类型直接抛 ValueError。写盘前先 tomllib.loads 自校验一遍，并
    写入 .tmp 再替换，保证校验失败时原文件不被破坏（测号中途写 start 尤其需要这个）。
    """
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix in ('.yaml', '.yml'):
        import yaml
        text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
    elif suffix == '.json':
        text = json.dumps(cfg, ensure_ascii=False, indent=2) + '\n'
    elif suffix == '.toml':
        import tomllib

        def value(item):
            if isinstance(item, bool):
                return 'true' if item else 'false'
            if isinstance(item, str):
                return json.dumps(item, ensure_ascii=False)
            if isinstance(item, (int, float)):
                return repr(item)
            if isinstance(item, list):
                return '[' + ', '.join(value(x) for x in item) + ']'
            if isinstance(item, dict):
                return '{' + ', '.join(json.dumps(k, ensure_ascii=False) + ' = ' + value(v)
                                       for k, v in item.items()) + '}'
            raise ValueError('TOML 无法保存此类型，请使用 config.yaml。')
        text = '\n'.join(json.dumps(k, ensure_ascii=False) + ' = ' + value(v)
                         for k, v in cfg.items()) + '\n'
        tomllib.loads(text)  # Check serialization before replacing the current file.
    else:
        raise ValueError('请选择 .yaml、.yml、.json 或 .toml 配置文件。')
    temporary = target.with_suffix(target.suffix + '.tmp')
    try:
        with temporary.open('w', encoding='utf8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
