// core.cpp - native engine for the URL shortener.
// Everything here lives in an anonymous namespace (hidden); only the
// extern "C" functions at the bottom are visible to Python (via ctypes).
// ctypes releases the GIL during these calls, so threads run truly parallel.
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <list>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace {

using Clock = std::chrono::steady_clock;

constexpr uint64_t modinv(uint64_t a) {  // Newton iteration mod 2^64
    uint64_t x = a;
    for (int i = 0; i < 6; ++i) x *= 2 - a * x;
    return x;
}

// ---------------------------------------------------------------- IdCodec
// DB id -> 7-char base62 code. Multiplying by an odd constant mod 2^40 is a
// bijection, so codes are unique, non-sequential (hard to enumerate) and
// reversible without storing the code in the database.
class IdCodec {
    static constexpr char kAlpha[] =
        "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";
    static constexpr uint64_t kMask = (1ULL << 40) - 1;
    static constexpr uint64_t kMul = 0x9E3779B97FULL;  // odd
    static constexpr uint64_t kInv = modinv(kMul);

public:
    static constexpr int kLen = 7;  // 62^7 > 2^40

    static void encode(uint64_t id, char* out) {
        uint64_t x = (id * kMul) & kMask;
        for (int i = kLen - 1; i >= 0; --i) { out[i] = kAlpha[x % 62]; x /= 62; }
        out[kLen] = '\0';
    }
    static bool decode(const char* s, uint64_t* id) {
        if (!s || std::strlen(s) != (size_t)kLen) return false;
        uint64_t v = 0;
        for (int i = 0; i < kLen; ++i) {
            const char* p = static_cast<const char*>(std::memchr(kAlpha, s[i], 62));
            if (!p) return false;
            v = v * 62 + (uint64_t)(p - kAlpha);
        }
        if (v > kMask) return false;
        *id = (v * kInv) & kMask;
        return *id != 0;
    }
};

// -------------------------------------------------------------- UrlValidator
class UrlValidator {
    static bool starts_ci(const std::string& s, const char* p) {
        for (size_t i = 0; p[i]; ++i) {
            if (i >= s.size()) return false;
            char c = s[i];
            if (c >= 'A' && c <= 'Z') c += 32;
            if (c != p[i]) return false;
        }
        return true;
    }
public:
    static bool ok(const char* u) {
        if (!u) return false;
        std::string s(u);
        if (s.size() < 10 || s.size() > 2048) return false;
        for (unsigned char c : s) if (c <= 0x20 || c == 0x7f) return false;
        size_t p;
        if (starts_ci(s, "http://")) p = 7;
        else if (starts_ci(s, "https://")) p = 8;
        else return false;
        if (p >= s.size()) return false;
        char h = s[p];
        return !(h == '/' || h == '?' || h == '#' || h == ':' || h == '@');
    }
};

// ----------------------------------------------------- Sharded LRU + TTL cache
// 16 independent shards, each with its own mutex: threads hitting different
// keys rarely contend. O(1) get/put via list + hash map.
class LruShard {
    struct Entry { std::string key, val; Clock::time_point exp; };
    std::mutex mu_;
    std::list<Entry> lru_;
    std::unordered_map<std::string, std::list<Entry>::iterator> idx_;
    size_t cap_;
public:
    explicit LruShard(size_t cap) : cap_(cap) {}
    bool get(const std::string& k, std::string& out, Clock::time_point now) {
        std::lock_guard<std::mutex> g(mu_);
        auto it = idx_.find(k);
        if (it == idx_.end()) return false;
        if (it->second->exp != Clock::time_point{} && it->second->exp < now) {
            lru_.erase(it->second); idx_.erase(it); return false;
        }
        lru_.splice(lru_.begin(), lru_, it->second);  // mark most-recent
        out = it->second->val;
        return true;
    }
    void put(const std::string& k, const std::string& v, Clock::time_point exp) {
        std::lock_guard<std::mutex> g(mu_);
        auto it = idx_.find(k);
        if (it != idx_.end()) {
            it->second->val = v; it->second->exp = exp;
            lru_.splice(lru_.begin(), lru_, it->second);
            return;
        }
        lru_.push_front({k, v, exp});
        idx_[k] = lru_.begin();
        if (idx_.size() > cap_) { idx_.erase(lru_.back().key); lru_.pop_back(); }
    }
    void erase(const std::string& k) {
        std::lock_guard<std::mutex> g(mu_);
        auto it = idx_.find(k);
        if (it != idx_.end()) { lru_.erase(it->second); idx_.erase(it); }
    }
    size_t size() { std::lock_guard<std::mutex> g(mu_); return idx_.size(); }
};

class Cache {
    static constexpr size_t kShards = 16;
    std::vector<std::unique_ptr<LruShard>> shards_;
    std::chrono::seconds ttl_;
    std::atomic<uint64_t> hits_{0}, misses_{0};
    LruShard& shard(const std::string& k) { return *shards_[std::hash<std::string>{}(k) % kShards]; }
public:
    Cache(size_t capacity, int ttl_sec) : ttl_(ttl_sec) {
        size_t per = (capacity + kShards - 1) / kShards;
        for (size_t i = 0; i < kShards; ++i) shards_.emplace_back(new LruShard(per ? per : 1));
    }
    bool get(const std::string& k, std::string& out) {
        bool f = shard(k).get(k, out, Clock::now());
        (f ? hits_ : misses_).fetch_add(1, std::memory_order_relaxed);
        return f;
    }
    void put(const std::string& k, const std::string& v) {
        shard(k).put(k, v, ttl_.count() > 0 ? Clock::now() + ttl_ : Clock::time_point{});
    }
    void erase(const std::string& k) { shard(k).erase(k); }
    void stats(uint64_t* o) {
        o[0] = hits_; o[1] = misses_;
        uint64_t n = 0; for (auto& s : shards_) n += s->size(); o[2] = n;
    }
};

// --------------------------------------------------------- Token-bucket limiter
class RateLimiter {
    struct Bucket { double tokens; Clock::time_point last; };
    struct Shard { std::mutex mu; std::unordered_map<std::string, Bucket> m; };
    static constexpr size_t kShards = 16;
    static constexpr size_t kPrune = 50000;
    Shard shards_[kShards];
    double rate_, burst_;
public:
    RateLimiter(double rate, double burst) : rate_(rate), burst_(burst) {}
    bool allow(const std::string& key) {
        Shard& s = shards_[std::hash<std::string>{}(key) % kShards];
        auto now = Clock::now();
        std::lock_guard<std::mutex> g(s.mu);
        if (s.m.size() > kPrune) {  // drop idle (fully refilled) buckets
            for (auto it = s.m.begin(); it != s.m.end();) {
                double dt = std::chrono::duration<double>(now - it->second.last).count();
                it = (it->second.tokens + dt * rate_ >= burst_) ? s.m.erase(it) : std::next(it);
            }
        }
        auto it = s.m.find(key);
        if (it == s.m.end()) it = s.m.emplace(key, Bucket{burst_, now}).first;
        Bucket& b = it->second;
        double dt = std::chrono::duration<double>(now - b.last).count();
        b.tokens = std::min(burst_, b.tokens + dt * rate_);
        b.last = now;
        if (b.tokens < 1.0) return false;
        b.tokens -= 1.0;
        return true;
    }
};

}  // namespace

// ------------------------------------------------------------------ C ABI
extern "C" {
void  sc_encode(uint64_t id, char* out8) { IdCodec::encode(id, out8); }
int   sc_decode(const char* code, uint64_t* id) { return IdCodec::decode(code, id) ? 1 : 0; }
int   sc_validate_url(const char* u) { return UrlValidator::ok(u) ? 1 : 0; }

void* sc_cache_new(size_t cap, int ttl) { return new Cache(cap, ttl); }
void  sc_cache_free(void* h) { delete static_cast<Cache*>(h); }
int   sc_cache_get(void* h, const char* k, char* buf, size_t n) {
    std::string v;
    if (!static_cast<Cache*>(h)->get(k, v)) return -1;
    if (v.size() + 1 > n) return -2;
    std::memcpy(buf, v.c_str(), v.size() + 1);
    return (int)v.size();
}
void  sc_cache_put(void* h, const char* k, const char* v) { static_cast<Cache*>(h)->put(k, v); }
void  sc_cache_del(void* h, const char* k) { static_cast<Cache*>(h)->erase(k); }
void  sc_cache_stats(void* h, uint64_t* out3) { static_cast<Cache*>(h)->stats(out3); }

void* sc_rl_new(double rate, double burst) { return new RateLimiter(rate, burst); }
void  sc_rl_free(void* h) { delete static_cast<RateLimiter*>(h); }
int   sc_rl_allow(void* h, const char* key) { return static_cast<RateLimiter*>(h)->allow(key) ? 1 : 0; }
}
