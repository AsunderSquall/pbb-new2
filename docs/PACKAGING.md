# Distribution and Rebuild

The Windows x64 executable release targets the x86-64 baseline (SSE2).
It does not require AVX, AVX2, AVX-512, or an Intel CPU. It requires a
64-bit Windows version supported by the bundled Python/Tk runtime.

Start PBBMonitor.exe inside the extracted PBBMonitor directory. Keep the
_internal directory alongside it. The engine's source and headers are bundled.
Selecting Rebuild compiles a host-optimized engine using an installed compiler.
The Python extension remains the conservative release build. A compiler is not
required for normal use of the executable package.

The pure ZIP contains PBBMonitor/, source/, config/ and launchers; source/build/
contains the portable ordinary/UI engines and Python core. The independent
environment ZIP contains only runtime/ (complete Python and GCC), licenses and
README_ENVIRONMENT.md. It is a reusable component, not a delta or an application
bundle. Existing environment users only update pure; new full-feature users
extract both ZIPs into the same parent directory, merging their PBB/ folders.
runtime/, source/, config/ and PBBMonitor/ must be siblings. Users with a compiler
are strongly encouraged to Rebuild once on their own machine for performance.
Install Python 3.10+, pybind11, PyYAML, and a C++17 compiler to rebuild:

```powershell
python -m pip install pybind11 PyYAML
python main.py -c config.example.yaml --rebuild
python ui.py
```

The development repository root is the packaging root: main.py, build.py, ui.py,
src/, fonts/, offline/ and config/ sit directly in it, with no extra source/ layer.
package_release.py chdirs to that root and reads every input relative to it, so run
the packagers from the repository root. README.pdf and 先读我.pdf are external
release inputs: export them from the Markdown files before packaging (see REPO.md).

Rebuild uses the native build mode by default. GCC uses -march=native;
MSVC chooses SIMD only after CPU and operating-system checks. Intel icpx is
optional. Native binaries are only reused on the build host. Portable binaries
can be reused on another Windows x64 host with matching source and file hashes.

To produce pure with Python 3.12 (matching PBBMonitor.spec):

```powershell
python -m pip install pyinstaller pybind11 PyYAML
python package_release.py
```

Run `python package_offline.py` to produce both independent components and verify
the merged layout with the bundled Python/compiler. Archive names are
PBB-windows-x64-pure.zip and PBB-windows-x64-environment.zip.

PBB_BUILD_MODE=portable requests the conservative build mode and forbids
PBB_CXXFLAGS overrides. Normal builds default to PBB_BUILD_MODE=native.
Default configs use the eight production scoring families, including CQD=4000.

Every release starts with `build_config.json` set to `tested: false` and
`status: "none"`, with the existing default engine options. Packaging replaces
any local tested selection with this fresh configuration. Portable builds ignore
local tuning choices.

In source/ or the offline package, run tune_quick.bat or tune_full.bat and enter
the previous full-task speed in T/day (a decimal is accepted) and the desired
test duration in minutes. Compilation is timed separately and excluded from
this soft target; testing continues after it expires. Recommended duration is
20 minutes. Initial candidate volume uses 90% of the entered throughput.
A completed run publishes the cumulative, verified selection to
config/build_config.json. All editable configs are in the outer PBB/config/:
config.yaml, config_football.yaml, config.example.* and build_config.json.
There are no extra config copies in source/ or PBBMonitor/_internal/.
Native Rebuild reads config/build_config.json for both ordinary and UI engines.
Testing alone does not install engines: users must Rebuild afterward to apply it.
Machine/compiler/source
checks prevent another host from silently using the selection. See AUTOTUNE.md
for installation, rollback, and isolated evaluation.

The editor offers "保存到当前文件" (preserving YAML/JSON/TOML format) and
"保存到 config.yaml" (writing config/config.yaml and selecting it). Charset types
are multiple-choice checkboxes; custom/Unicode inputs appear only when selected.
