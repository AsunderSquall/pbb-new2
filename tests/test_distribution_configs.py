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

    def test_outer_config_word_files_and_archive_payload_match(self):
        configs=package_release.distribution_configs()
        self.assertIn('config_words_sequential.yaml',configs)
        self.assertFalse(json.loads(configs['build_config.json'])['tested'])
        paths.ensure_build_dir()
        with tempfile.TemporaryDirectory(dir=paths.BUILD_DIR) as directory:
            outer=Path(directory)
            package_release.stage_configs(outer)
            cfg=ui.load_config(str(outer/'config/config_words_sequential.yaml'))
            _validate_config(cfg)
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


if __name__=='__main__':unittest.main()
