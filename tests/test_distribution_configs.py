"""随附测号配置包含PP/PD，词族示例与词库在发布布局中可直接加载。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tools'))
import package_release
from pbb import engine, flex, paths, ui
from pbb.main import _build_task_config, _validate_config


class DistributionConfigTests(unittest.TestCase):
    def test_all_bundled_search_configs_include_pp_and_pd(self):
        files=[ROOT/'config'/n for n in package_release.CONFIG_NAMES if n!='build_config.json']
        linux=ROOT/'pbb_linux/config'
        if linux.is_dir():
            files.extend(p for p in linux.glob('config*') if p.suffix in ('.yaml','.json','.toml'))
        checked=0
        for path in files:
            if not path.is_file():continue
            with self.subTest(config=path):
                cfg=ui.load_config(str(path))
                collection=cfg['collection']
                self.assertEqual(collection['min_pp'],8000)
                self.assertEqual(collection['min_pd'],8800)
                self.assertTrue({'pp','pd'}<=set(ui._normalize_score_list(collection['score_list'])))
                _validate_config(cfg)
                checked+=1
        self.assertGreaterEqual(checked,6)

    def test_bundled_configs_never_hardcode_a_machine_thread_count(self):
        """随包配置的线程数必须是 -1（自动），不能带某一台机器的具体值。

        7.0.0 的 config_football.yaml 写死过 worker_threads: 40，而发布的 config.yaml
        正是由它生成 —— 于是所有用户都在用 40 线程，任何机器上都一样：核少的机器过载
        降频，核多的机器用不满，测速数据也不可与别的机器比较。pbb_linux 的配置是从
        那份文件复制的，同样必须保持 -1。这条用例把该回退钉住。
        """
        files=[ROOT/'config'/n for n in package_release.CONFIG_NAMES if n!='build_config.json']
        linux=ROOT/'pbb_linux/config'
        if linux.is_dir():
            files.extend(p for p in linux.glob('config*') if p.suffix in ('.yaml','.json','.toml'))
        checked=0
        for path in files:
            if not path.is_file():continue
            with self.subTest(config=path):
                cfg=ui.load_config(str(path))
                self.assertEqual(cfg.get('threads',{}).get('worker_threads',-1),-1,
                                 f'{path} 写死了线程数；随包配置必须是 -1（自动）')
                checked+=1
        self.assertGreaterEqual(checked,6)

    def test_outer_config_word_files_and_archive_payload_match(self):
        configs=package_release.distribution_configs()
        self.assertIn('config_words_sequential.yaml',configs)
        # 测号配置里不再夹带 build_config.json：它是应用设置，进 settings/。
        self.assertNotIn('build_config.json',configs)
        self.assertFalse(json.loads(package_release.distribution_settings()['build_config.json'])['tested'])
        paths.ensure_build_dir()
        with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
            outer=Path(directory)
            package_release.stage_configs(outer)
            cfg=ui.load_config(str(outer/'config/config_words_sequential.yaml'))
            _validate_config(cfg)
            self.assertTrue((outer/'settings/build_config.json').is_file())
            self.assertFalse((outer/'config/build_config.json').exists())
            self.assertEqual(cfg['enumeration']['mode'],1)
            self.assertEqual(cfg['prefixes'],[{'name':''}])
            self.assertEqual(cfg['suffixes'],[{'name':''}])
            task=_build_task_config(cfg)
            template,slots=flex.build_layout(task,engine._build_charset)
            self.assertEqual(len(slots),4)
            self.assertEqual([width for _,width,_ in slots],[6]*4)
            body=bytearray(template)
            for offset,width,data in slots:body[offset:offset+width]=data[:width]
            self.assertEqual(body.decode('utf8'),'重要的经济必须地发展')
            for name,data in package_release.distribution_charsets().items():
                self.assertEqual((outer/'charsets'/name).read_bytes(),data)
            self.assertEqual((outer/'config/config_words_sequential.yaml').read_bytes(),configs['config_words_sequential.yaml'])

    def test_ui_settings_is_shipped_with_the_football_thresholds(self):
        """ui_settings.json 要随包发布：启动默认配置 + 新配置的默认阈值。"""
        import yaml
        settings = package_release.distribution_settings()
        shipped = json.loads(settings['ui_settings.json'])
        football = yaml.safe_load(
            (Path(package_release.ROOT) / 'config/config_football.yaml').read_text(encoding='utf8'))
        self.assertEqual(shipped['collection'], football['collection'])
        self.assertEqual(shipped['default_config'], 'config_football.yaml')

    def test_release_archive_names_end_with_the_version(self):
        """归档名必须以 -<版本号> 结尾，版本号来自仓库根 VERSION。"""
        import re
        version = package_release.release_version()
        self.assertRegex(version, r'^\d+\.\d+\.\d+$')
        self.assertEqual(version, (Path(package_release.ROOT) / 'VERSION').read_text(encoding='utf8').strip())
        for prefix in ('PBB-windows-x64-pure', 'PBB-windows-x64-environment'):
            name = f'{prefix}-{version}.zip'
            self.assertTrue(re.fullmatch(rf'{re.escape(prefix)}-\d+\.\d+\.\d+\.zip', name))

    def test_bad_version_is_refused(self):
        with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
            root = Path(directory)
            from unittest.mock import patch
            with patch.object(package_release, 'ROOT', root):
                with self.assertRaises(SystemExit):
                    package_release.release_version()          # 缺文件
                (root / 'VERSION').write_text('7.0\n', encoding='utf8')
                with self.assertRaises(SystemExit):
                    package_release.release_version()          # 格式不对


class CombinedArchiveTests(unittest.TestCase):
    """发布产物必须同时给出 pure、environment 和「两者合一」的单包。"""

    def test_combined_archive_is_named_pbb_version(self):
        import package_offline
        version = package_release.release_version()
        path = package_offline.combined_archive_path()
        self.assertEqual(path.name, f'pbb-{version}.zip', '合并包名必须是 pbb-<版本>.zip')
        self.assertEqual(path.parent.name, 'release')

    def test_combined_archive_contains_both_halves(self):
        import zipfile
        import package_offline
        with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
            root = Path(directory) / 'PBB'
            (root / 'source').mkdir(parents=True)
            (root / 'source' / 'main.py').write_text('x', encoding='utf8')
            (root / 'runtime' / 'python').mkdir(parents=True)
            (root / 'runtime' / 'python' / 'python.exe').write_bytes(b'MZ')
            (root / 'source' / '__pycache__').mkdir()
            (root / 'source' / '__pycache__' / 'x.pyc').write_bytes(b'x')
            archive = package_offline.write_combined_archive(root, Path(directory) / 'pbb.zip')
            names = set(zipfile.ZipFile(archive).namelist())
            self.assertIn('PBB/source/main.py', names, '合并包应含应用本体')
            self.assertIn('PBB/runtime/python/python.exe', names, '合并包应含随包运行时')
            self.assertFalse([n for n in names if '__pycache__' in n or n.endswith('.pyc')],
                             '合并包不应带字节码缓存')


if __name__=='__main__':unittest.main()
