// bridge.cpp — pybind11 module: exposes character set data to Python.
//
// 中文总述: 这是 Python 与 C++ 之间唯一的「数据桥」, 编译产物是 pbb_core.pyd/so。
//   它只做一件事: 把 charset_data.hpp 的内置字符表和各种加载/编码函数暴露给 Python。
//   要点:
//     * 它不参与真实测号。实际评分由独立的 C++ 子进程 (engine_main.cpp) 完成, 因此本模块
//       里的全局缓冲区与子进程里的同名缓冲区是两个独立副本, 互不影响。
//     * 本模块自身的全局状态 (charset/hanzi_cnt 等) 不是线程安全的, Python 侧需自行保证
//       加载顺序 (先 init_exhanzi / reset_charset, 再 load_*), 再做并发读取。
//     * 所有 get_*_bytes 都返回「定长数组的原始字节」, 长度由 sizeof 决定, 因此调用方
//       (engine.py) 必须知道每张表的字符数与字节宽度才能正确切分。
#include <pybind11/pybind11.h>
#include "charset_data.hpp"
#include "charset.hpp"

namespace py = pybind11;

PYBIND11_MODULE(pbb_core, m) {
    m.doc() = "PBB charset data provider";

    // ---- 内置定长字符表导出 (hanzi_/xila/XILA/ewen/EWEN/lading/LADING/pingjia/pianjia/mangwen) ----
    // 全部按 sizeof 原样返回: hanzi_ 是 3500 个常用汉字 (3 字节/字), 希腊/俄文/拉丁是 2 字节表,
    // 平假名/片假名/盲文是 3 字节表。注意 signed char 数组直接按字节 reinterpret, 不做符号处理。
    m.def("get_hanzi_bytes", []() {
        return py::bytes(reinterpret_cast<const char*>(hanzi_), sizeof(hanzi_)); });
    m.def("get_xila_bytes", []() {
        return py::bytes(reinterpret_cast<const char*>(xila), sizeof(xila)); });
    m.def("get_XILA_bytes", []() {
        return py::bytes(reinterpret_cast<const char*>(XILA), sizeof(XILA)); });
    m.def("get_ewen_bytes", []() {
        return py::bytes(reinterpret_cast<const char*>(ewen), sizeof(ewen)); });
    m.def("get_EWEN_bytes", []() {
        return py::bytes(reinterpret_cast<const char*>(EWEN), sizeof(EWEN)); });
    m.def("get_lading_bytes", []() {
        return py::bytes(reinterpret_cast<const char*>(lading), sizeof(lading)); });
    m.def("get_LADING_bytes", []() {
        return py::bytes(reinterpret_cast<const char*>(LADING), sizeof(LADING)); });
    m.def("get_pingjia_bytes", []() {
        return py::bytes(reinterpret_cast<const char*>(pingjia), sizeof(pingjia)); });
    m.def("get_pianjia_bytes", []() {
        return py::bytes(reinterpret_cast<const char*>(pianjia), sizeof(pianjia)); });
    m.def("get_mangwen_bytes", []() {
        return py::bytes(reinterpret_cast<const char*>(mangwen), sizeof(mangwen)); });
    // 扩展汉字: 必须先调用 init_exhanzi() 填充, 否则拿到的是全 0 数组。
    // hanzi_3 是 3 字节宽的扩展汉字表, hanzi_4 是 4 字节宽的 (两者容量精确配套, 见 charset.hpp)。
    m.def("get_extended_hanzi_3_bytes", []() {
        return py::bytes(reinterpret_cast<const char*>(hanzi_3), sizeof(hanzi_3)); });
    m.def("get_extended_hanzi_4_bytes", []() {
        return py::bytes(reinterpret_cast<const char*>(hanzi_4), sizeof(hanzi_4)); });

    // ---- 字符集构建接口 (会修改模块内全局状态) ----
    m.def("init_exhanzi", &init_exhanzi);
    // load_hanzi 只接受 BMP 3 字节区间; 传入 4 字节字符会打告警并破坏「固定宽度」假设。
    m.def("load_hanzi", &load_hanzi, py::arg("start"), py::arg("end"));
    // 单码点 → UTF-8 字节; 非法码点返回长度 0 的 bytes (调用方需判空)。
    m.def("encode_unicode", [](int cp) -> py::bytes {
        char buf[4]; int len = load_unicode_codepoint(cp, buf);
        return py::bytes(buf, len); }, py::arg("codepoint"));

    // 已加载缓冲区的原始字节与字符数: 长度取 charset_cnt+1 是配合「空为 -1」的哨兵,
    // 因此空缓冲区返回 0 字节 (而不是读越界)。charset_len 是字符数, 不是字节数。
    m.def("get_charset_bytes", []() { return py::bytes(charset, charset_cnt + 1); });
    m.def("get_charset_len", []() { return charset_len; });
    m.def("reset_charset", &reset_charset);
    // 任意 Unicode 区间 (允许 1~4 字节混合); 与 load_hanzi 一样是追加语义。
    m.def("load_unicode_range", &load_unicode_range, py::arg("start"), py::arg("end"));
}
