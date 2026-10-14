#pragma once
// 格式层只生成名字字节；评分、收集、排行榜和任务控制继续共用 engine.hpp。
struct FlexSlot {
    int offset, width;
    std::string bytes;
    uint64_t radix() const { return bytes.size() / width; }
};
struct FlexLayout {
    bool enabled = false;
    std::string text;
    std::vector<FlexSlot> slots;
    uint64_t total = 1, low_total = 1;
    size_t low_begin = 0;

    bool parse(const std::unordered_map<std::string,std::string>& kv) {
        enabled = kv.count("flex_template");
        if (!enabled) return true;
        try {
            text = hex_decode(kv.at("flex_template"));
            auto offsets = split_csv(kv.at("flex_offsets"));
            auto widths = split_csv(kv.at("flex_widths"));
            auto tables = split_csv(kv.at("flex_charsets"));
            if (offsets.empty() || offsets.size() != widths.size() || offsets.size() != tables.size()) return false;
            size_t end = 0;
            for (size_t i = 0; i < offsets.size(); ++i) {
                FlexSlot slot{std::stoi(offsets[i]), std::stoi(widths[i]), hex_decode(tables[i])};
                if (slot.offset < 0 || slot.width < 1 || slot.bytes.empty() ||
                    slot.bytes.size() % slot.width || slot.bytes.find('\0') != std::string::npos ||
                    size_t(slot.offset) < end || size_t(slot.offset) > text.size() ||
                    size_t(slot.width) > text.size() - size_t(slot.offset)) return false;
                end = size_t(slot.offset) + size_t(slot.width);
                auto radix = slot.radix();
                total = total > UINT64_MAX / radix ? UINT64_MAX : total * radix;
                slots.push_back(std::move(slot));
            }
            if (text.size() >= 512) return false;
            low_begin = slots.size();
            while (low_begin > 0 && low_total < CHUNK_SIZE) {
                auto radix = slots[--low_begin].radix();
                low_total = low_total > UINT64_MAX / radix ? UINT64_MAX : low_total * radix;
            }
            return true;
        } catch (const std::exception&) { return false; }
    }
};
