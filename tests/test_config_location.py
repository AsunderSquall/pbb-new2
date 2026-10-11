import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

# 测试文件位于 tests/ 子目录，被测代码在 pbb/ 包里；先把仓库根与 tools/ 加入 sys.path。
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / 'tools'))

from pbb import _build as build
from pbb import autotune
from pbb import build_settings
import package_release  # tools/ 下的打包工具
from pbb import ui


# 测试把临时目录建在 build/ 下，所以要保证它存在（干净检出后无需先构建）。
from pbb import paths as _paths
_paths.ensure_build_dir()

class ConfigLocationTests(unittest.TestCase):
    # 本组用例守护“配置只有一份、且永远在外层 config/”这条布局约定，以及编辑器
    # 保存配置时的目标选择和格式保持。
    # 发布布局里源码在内层 source/，但构建配置必须始终写在外层 config/build_config.json，
    # 回退后要逐字节还原成安装前的版本。防的是安装流程在内层又生成一份
    # source/build_config.json，让监控器和 CLI 各读各的。
    # 夹具伪造结果目录（selection.json + 新旧二进制 + 源码哈希），
    # patch 掉 autotune.ROOT / 机器指纹 / 清单写入，避免动真实目录。
    def test_offline_install_and_rollback_keep_outer_build_config(self):
        with tempfile.TemporaryDirectory(dir=build.BUILD_DIR) as directory:
            outer=Path(directory);source=outer/'source';(source/'build').mkdir(parents=True)
            # 发布布局判据是"source/ 里有 pbb 包"，夹具必须造出来，
            # 否则 package_root 不会从 source/ 上浮到外层包根。
            (source/'pbb').mkdir();(source/'pbb'/'__init__.py').write_text('')
            (outer/'PBBMonitor').mkdir();run=source/'trial';run.mkdir()
            (source/'engine_main.cpp').write_text('unchanged')
            build_settings.write(outer/'config/build_config.json',build_settings.fresh())
            previous=(outer/'config/build_config.json').read_bytes()
            selected={}
            for name,file in [('standard','pbb_engine.exe'),('ui','pbb_ui_engine.exe')]:
                target=source/'build'/file;target.write_bytes(b'old')
                Path(str(target)+'.build.json').write_bytes(b'old manifest')
                binary=run/file;binary.write_bytes(b'new')
                selected[name]=dict(binary=file,sha256=autotune.digest(binary),flags=['-O3'])
            autotune.save(run/'selection.json',dict(status='complete',machine=['test'],selected=selected,
                source_hashes={'engine_main.cpp':autotune.digest(source/'engine_main.cpp')},compiler='g++'))
            def record(path,*args,**kwargs):Path(path+'.build.json').write_text('new manifest')
            with patch.object(autotune,'ROOT',source),patch.object(build,'_machine_identity',return_value=['test']), \
                 patch.object(build,'_record_binary',side_effect=record),patch.dict('os.environ',{'PBB_SRC_DIR':''}):
                autotune.install(run)
                self.assertTrue(json.loads((outer/'config/build_config.json').read_text())['tested'])
                self.assertFalse((source/'build_config.json').exists())
                autotune.install(run,rollback=True)
            self.assertEqual((outer/'config/build_config.json').read_bytes(),previous)
            self.assertEqual((source/'build/pbb_ui_engine.exe').read_bytes(),b'old')

    # 打包时 stage_configs 把可编辑配置集中到外层 config/，并清掉根、source/、
    # PBBMonitor/_internal/ 下的同名副本；发布的 build_config.json 必须未测试。
    # 这是 REPO.md §7.2/§7.3 两个缺陷的回归：漏写 build_config.json，或清理时把
    # 内层 source/ 分支删掉，都会在这里暴露。
    def test_package_configs_only_at_outer_level(self):
        with tempfile.TemporaryDirectory(dir=build.BUILD_DIR) as directory:
            root=Path(directory)
            # 先在各层铺满旧配置，模拟历史残留。source/ 里要有 pbb 包，
            # 才算"发布布局的源码目录"（package_root 的上浮判据）。
            (root/'source'/'pbb').mkdir(parents=True)
            (root/'source'/'pbb'/'__init__.py').write_text('')
            for folder in ('source','PBBMonitor/_internal'):
                (root/folder).mkdir(parents=True, exist_ok=True)
                for name in package_release.CONFIG_NAMES:(root/folder/name).write_text('old')
            package_release.stage_configs(root)
            self.assertTrue((root/'config/config.yaml').is_file())
            self.assertFalse(json.loads((root/'config/build_config.json').read_text())['tested'])
            for folder in ('source','PBBMonitor/_internal'):
                self.assertFalse(any((root/folder/name).exists() for name in package_release.CONFIG_NAMES))
            self.assertEqual(build_settings.config_root(root/'source'),root/'config')
            self.assertEqual(build_settings.config_root(root/'PBBMonitor/_internal'),root/'config')

    # 编辑器两个保存选项的边界：保存到当前文件只动该文件；保存到 config.yaml 写到共用
    # 配置目录、不碰原文件，并把 UI 的当前配置切到 config.yaml。两种情况都不该弹错误框。
    # 夹具不建真窗口：用 Mock/SimpleNamespace 顶替 config_var、确认弹窗与控件同步。
    def test_editor_current_and_default_destinations(self):
        cfg=ui.load_config('config_football.yaml')
        with tempfile.TemporaryDirectory(dir=build.BUILD_DIR) as directory:
            root=Path(directory);current=root/'custom.json'
            state={'path':str(current)}
            variable=Mock();variable.get.side_effect=lambda:state['path']
            variable.set.side_effect=lambda value:state.update(path=value)
            owner=SimpleNamespace(_editor_config=Mock(return_value=cfg),editor=None,
                _confirm_affix_mismatch=Mock(return_value=True),config_var=variable,
                _sync_config_controls=Mock(),editor_cfg=None)
            # 必须用 patch.object(ui.messagebox, ...) 而不是 patch('pbb.ui.messagebox...')：
            # 字符串形式会经 pkgutil.resolve_name 重新导入 pbb.ui，而该模块底部有
            # `if __name__ == "__main__": main()`，被当作新模块导入不会触发它，
            # 但重新 import 仍可能再次执行模块级代码；用对象形式更精确也更安全。
            with patch.object(ui,'CONFIG_DIR',str(root)),patch.object(ui.messagebox,'showerror') as error:
                ui.Monitor._save_editor(owner)
                self.assertEqual(json.loads(current.read_text(encoding='utf8')),cfg)
                self.assertFalse((root/'config.yaml').exists())
                before=current.read_bytes()
                ui.Monitor._save_editor(owner,to_default=True)
                self.assertEqual(ui.load_config(str(root/'config.yaml')),cfg)
                self.assertEqual(current.read_bytes(),before)
                self.assertEqual(state['path'],str(root/'config.yaml'))
                error.assert_not_called()

    # 四种后缀（yaml/yml/json/toml）存了再读必须与内存中的配置等价：含中文、引号、
    # 反斜杠和换行的字段不能损坏。防的是编码/转义处理让配置悄悄变形。
    def test_yaml_json_toml_round_trip_preserves_nested_config(self):
        cfg=ui.load_config('config_football.yaml')
        cfg['team_name']='中文 "引号"\\换行\n队伍'
        with tempfile.TemporaryDirectory(dir=build.BUILD_DIR) as directory:
            for suffix in ('.yaml','.yml','.json','.toml'):
                path=Path(directory)/('current'+suffix)
                ui.save_config(path,cfg)
                self.assertEqual(ui.load_config(str(path)),cfg)

    # TOML 无法表达 None：保存失败要抛 ValueError，并且磁盘上的原文件必须一字未动。
    # 防的是“先清空/半途写入再报错”，把用户现有配置毁掉。
    def test_failed_toml_save_preserves_current_file(self):
        with tempfile.TemporaryDirectory(dir=build.BUILD_DIR) as directory:
            path=Path(directory)/'current.toml';path.write_text('old')
            with self.assertRaises(ValueError):ui.save_config(path,{'unsupported':None})
            self.assertEqual(path.read_text(),'old')

    # 回归：配置校验失败时，编辑器必须"弹窗并留在原地"，不能把监控器进程干掉。
    #
    # 原来的 _die() 直接 sys.exit(1)，抛出的 SystemExit 继承自 BaseException，
    # 而 _save_editor / _open_editor / _start / _choose_config 用的都是 except Exception，
    # 于是异常一路逃到 Tk 回调之外、进程退出 —— 用户看到的就是"保存时闪退"，
    # 既没有提示、也不知道哪里填错了。
    # 现在 _die() 抛 ConfigError（继承 Exception），各调用处既能捕获也能显示原因。
    def test_invalid_config_shows_error_instead_of_killing_process(self):
        cfg = ui.load_config("config_football.yaml")
        # min_cqd>0 但 score_list 里没有 cqd：门控会失效，属被明确拒绝的配置。
        cfg["collection"]["min_cqd"] = 4000
        cfg["collection"]["score_list"] = "qp,qd,cp,fp,wc,fs,pj"
        cfg["enumeration"]["mode"] = 4           # 绕开一一对应配对提示
        cfg["prefixes"] = [{"name": "A"}]
        cfg["suffixes"] = [{"name": "X"}]

        with tempfile.TemporaryDirectory(dir=build.BUILD_DIR) as directory:
            target = Path(directory) / "out.toml"
            variable = Mock()
            variable.get.return_value = str(target)
            owner = SimpleNamespace(
                _editor_config=Mock(return_value=cfg), editor=None,
                _confirm_affix_mismatch=Mock(return_value=True),
                config_var=variable, _sync_config_controls=Mock(),
                _close_editor=Mock(), _start=Mock(), editor_cfg=None)

            # 这里不 mock _validate_config：走真实校验，任何异常一旦逃出 _save_editor
            # 都会让测试直接报错（而不是被 except 静默吃掉），所以它同时守住两件事 ——
            # 校验确实拒绝了这份配置，且拒绝的方式是"弹窗"而不是"退出进程"。
            with patch.object(ui.messagebox, "showerror") as error:
                ui.Monitor._save_editor(owner)

            error.assert_called_once()
            self.assertIn("无法保存配置", error.call_args[0][0])
            self.assertIn("min_cqd", error.call_args[0][1])
            self.assertFalse(target.exists(), "校验失败时不应写出配置文件")
            owner._start.assert_not_called()


if __name__=='__main__':unittest.main()
