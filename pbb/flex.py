"""具名字符集与格式列表；只负责候选布局，不参与评分或门控。"""
import copy
import hashlib
import json
from pathlib import Path


def file_bytes(cs):
    """UTF-8 文件：lines 每行一个单元；text 每个 Unicode 字符一个单元。"""
    try:
        text = Path(cs['file']).read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError) as exc:
        raise ValueError(f'无法读取字符集文件 {cs.get("file")}: {exc}') from exc
    mode = cs.get('file_format', 'lines')
    if mode not in ('lines', 'text'):
        raise ValueError('file_format 必须为 lines 或 text')
    entries = text.splitlines() if mode == 'lines' else list(text.replace('\r', '').replace('\n', ''))
    width = cs['single_char_length']
    if not entries or any(len(x.encode('utf8')) != width or '\0' in x for x in entries):
        raise ValueError(f'字符集文件 {cs["file"]} 的每个单元须为 {width} 字节，不能为空或含 NUL')
    return b''.join(x.encode('utf8') for x in entries)


def resolve_files(cfg, config_path):
    """文件引用相对于配置文件；保存到其他目录时仍指向原文件。"""
    families = cfg.get('character_sets', {})
    for cs in families.values():
        if isinstance(cs, dict) and isinstance(cs.get('file'), str):
            path = Path(cs['file'])
            cs['file'] = str((Path(config_path).resolve().parent / path).resolve())
    return cfg


def resume_id(task, template, slots):
    # 范围和线程可变；候选、评分、收集和输出语义必须相同。
    ignored = {'character_set', 'character_sets', 'format', 'scl', 'variable_len',
               'range_L', 'range_R', 'prefix_range_L', 'prefix_range_R', 'n_threads',
               'ui_mode', 'output_log', 'output_speed', 'debug', 'result_file',
               'output_dest', 'output_dir', 'control_file'}
    semantic = {k: v for k, v in task.items() if k not in ignored}
    semantic['output_rule'] = 'qp-u-quadratic-82.48156669219784--0.24932235019037452-0.0001942206736111687-v4'
    semantic['layout'] = [template.hex(), [(o, w, d.hex()) for o, w, d in slots]]
    return hashlib.sha256(json.dumps(semantic, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def normalize_config(cfg):
    """为格式配置补充旧界面所需字段，返回副本，不改写用户配置。"""
    if 'format' not in cfg and 'character_sets' not in cfg:
        return cfg
    families = cfg.get('character_sets')
    fmt = cfg.get('format')
    if not isinstance(families, dict) or not families:
        raise ValueError('character_sets 必须为非空的具名字符集字典')
    for name, cs in families.items():
        if not isinstance(name, str) or not name or not isinstance(cs, dict):
            raise ValueError('字符集族必须使用非空名字和字符集配置')
        width = cs.get('single_char_length')
        if type(width) is not int or not 1 <= width <= 511:
            raise ValueError(f'字符集族 {name} 的 single_char_length 必须为 1..511')
        if 'file' in cs:
            if not isinstance(cs['file'], str) or not cs['file'] or any(k in cs for k in ('types', 'custom_values', 'unicode_ranges')):
                raise ValueError(f'字符集族 {name} 的 file 必须为路径，不能同时指定内置或自定义来源')
            file_bytes(cs)
            continue
        types = cs.get('types')
        maximum = {1: 5, 2: 8, 3: 8, 4: 3}.get(width, 2)
        if not isinstance(types, list) or not types or any(
                type(t) is not int or not 1 <= t <= maximum for t in types):
            raise ValueError(f'字符集族 {name} 的 types 不合法')
        custom_code = {1: (4, 5), 2: (7,), 3: (7,), 4: (2,)}.get(width, (1,))
        if any(t in custom_code for t in types):
            custom = cs.get('custom_values')
            if isinstance(custom, str):
                data = custom.encode('utf8')
                # 多字节单字符族不允许把不同宽度的 Unicode 字符拼成伪等宽单元。
                if width <= 4 and any(len(c.encode('utf8')) != width for c in custom):
                    raise ValueError(f'字符集族 {name} 含不同字节长度的字符')
            elif isinstance(custom, list) and all(type(x) is int and 0 <= x <= 255 for x in custom):
                data = bytes(custom)
            else:
                raise ValueError(f'字符集族 {name} 的 custom_values 必须为字符串或字节列表')
            if not data or len(data) % width or b'\0' in data:
                raise ValueError(f'字符集族 {name} 必须由非空、等宽且不含 NUL 的单元组成')
        for interval in cs.get('unicode_ranges', []):
            if not isinstance(interval, dict):
                raise ValueError(f'字符集族 {name} 的 Unicode 区间不合法')
            start, end = interval.get('start'), interval.get('end')
            if type(start) is not int or type(end) is not int or not 1 <= start <= end <= 0x10ffff:
                raise ValueError(f'字符集族 {name} 的 Unicode 区间越界')
            if start <= 0xdfff and end >= 0xd800:
                raise ValueError('Unicode 区间不能包含代理码点')
            if len(chr(start).encode('utf8')) != width or len(chr(end).encode('utf8')) != width:
                raise ValueError(f'字符集族 {name} 的 Unicode 区间必须等宽')
    if not isinstance(fmt, list) or not fmt:
        raise ValueError('format 必须为非空列表')
    count, length = 0, 0
    for item in fmt:
        if isinstance(item, str):
            if '\0' in item:
                raise ValueError('format 固定字符串不能包含 NUL')
            length += len(item.encode('utf8'))
        elif isinstance(item, dict) and set(item) == {'charset'} and isinstance(item['charset'], str):
            if item['charset'] not in families:
                raise ValueError(f'format 引用了不存在的字符集族：{item["charset"]}')
            length += families[item['charset']]['single_char_length']
            count += 1
        else:
            raise ValueError('format 元素必须为固定字符串或 {charset: 字符集族名字}')
    if count == 0:
        raise ValueError('format 至少需要一个可变位置')
    result = copy.deepcopy(cfg)
    result.setdefault('prefixes', [{'name': '+'}])
    result.setdefault('suffixes', [{'name': '+'}])
    for p in result['prefixes']:
        for s in result['suffixes']:
            affixes = ''.join(x['name'] if x['name'] != '+' else '' for x in (p, s))
            if '\0' in affixes or length + len(affixes.encode('utf8')) >= 512:
                raise ValueError('格式加前后缀必须少于 512 字节且不含 NUL')
    result.setdefault('enumeration', {})['variable_length'] = count
    result['character_set'] = {'single_char_length': 1, 'types': [1]}
    return result


def build_layout(task, build_charset):
    """将格式编译为模板字节、每个可变位置的偏移和等宽字符表。"""
    tables = {}
    for name, cs in task['character_sets'].items():
        data = bytes.fromhex(build_charset(cs))
        width = cs['single_char_length']
        if not data or len(data) % width or b'\0' in data:
            raise ValueError(f'字符集族 {name} 为空、非等宽或包含 NUL')
        tables[name] = (width, data)
    template = bytearray()
    slots = []
    for item in task['format']:
        if isinstance(item, str):
            template.extend(item.encode('utf8'))
        else:
            width, data = tables[item['charset']]
            slots.append((len(template), width, data))
            template.extend(bytes(width))
    return bytes(template), slots


def parse_rank(name, template, slots):
    """反推完整格式名字的混合进制编号；固定字节不匹配则返回 None。"""
    if len(name) != len(template):
        return None
    rank, cursor = 0, 0
    for offset, width, data in slots:
        if name[cursor:offset] != template[cursor:offset]:
            return None
        entries = [data[i:i+width] for i in range(0, len(data), width)]
        try:
            digit = entries.index(name[offset:offset+width])
        except ValueError:
            return None
        rank = rank * len(entries) + digit
        cursor = offset + width
    return rank if name[cursor:] == template[cursor:] else None
