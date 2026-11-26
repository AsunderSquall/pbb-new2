#pragma once

// 引擎的前后缀列表先按逗号分项，再解码每项的 UTF-8 字节。
// 空项保留原来的位置，旧版调用仍可以传未编码的 CSV。
static bool read_affix_param(
    const std::unordered_map<std::string, std::string>& params, const std::string& key,
    std::vector<std::string>& names) {
    const auto encoded = params.find(key + "_hex");
    if (encoded == params.end()) {
        const auto legacy = params.find(key);
        names = split_csv(legacy == params.end() ? "" : legacy->second);
        return true;
    }

    names = split_csv(encoded->second);
    for (auto& name : names) {
        if (name.size() % 2 || name.find_first_not_of("0123456789abcdefABCDEF") != std::string::npos) {
            fprintf(stderr, "[engine] ERROR: invalid %s_hex entry\n", key.c_str());
            return false;
        }
        name = hex_decode(name);
        if (name.find_first_of("\r\n\t\0", 0, 4) != std::string::npos) {
            fprintf(stderr, "[engine] ERROR: %s_hex entry contains a forbidden control character\n", key.c_str());
            return false;
        }
    }
    return true;
}
