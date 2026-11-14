// tiny C ABI over whisper.cpp + parakeet.cpp so Python (ctypes) can keep a model warm in-process.
#include "whisper.h"
#include "parakeet.h"
#include "ggml-backend.h"
#include <string>
#include <cstring>
extern "C" {
static void quiet(enum ggml_log_level, const char*, void*) {}
void asr_init_backends(int silent, const char* dir) {
    if (silent) { whisper_log_set(quiet, nullptr); parakeet_log_set(quiet, nullptr); }
    ggml_backend_load_all_from_path(dir);
}
// description of the idx-th GPU-type ggml device ("" if none): lets Python refuse a silent CPU fallback
const char* asr_gpu_name(int idx) {
    int k = 0;
    for (size_t i = 0; i < ggml_backend_dev_count(); i++) {
        ggml_backend_dev_t d = ggml_backend_dev_get(i); auto t = ggml_backend_dev_type(d);
        if (t == GGML_BACKEND_DEVICE_TYPE_GPU || t == GGML_BACKEND_DEVICE_TYPE_IGPU) { if (k++ == idx) return ggml_backend_dev_description(d); }
    }
    return "";
}
void* asr_whisper_load(const char* path, int use_gpu, int dev, int flash) {
    whisper_context_params p = whisper_context_default_params();
    p.use_gpu = use_gpu; p.gpu_device = dev; p.flash_attn = flash;
    return whisper_init_from_file_with_params(path, p);
}
static thread_local std::string out;
const char* asr_whisper_run(void* ctx, const float* pcm, int n, int threads, const char* lang, int opts) {
    whisper_full_params p = whisper_full_default_params(WHISPER_SAMPLING_GREEDY);
    p.n_threads = threads; p.language = lang; p.translate = false;
    p.print_progress = p.print_realtime = p.print_timestamps = p.print_special = false;
    p.no_timestamps = true; p.single_segment = false; p.no_context = true; p.suppress_nst = true;
    // opts bit0: allow timestamps decoding (more robust for multilingual large models); bit1: temperature fallback
    if (opts & 1) p.no_timestamps = false;
    if (!(opts & 2)) p.temperature_inc = 0.0f;
    p.max_tokens = (int)(n / 16000.0 * 8) + 24;  // cap runaway repetition
    p.single_segment = (n < 16000 * 28);
    out.clear();
    if (whisper_full((whisper_context*)ctx, p, pcm, n) != 0) return "";
    int ns = whisper_full_n_segments((whisper_context*)ctx);
    for (int i = 0; i < ns; i++) out += whisper_full_get_segment_text((whisper_context*)ctx, i);
    size_t a = out.find_first_not_of(' '); out = (a == std::string::npos) ? "" : out.substr(a);
    return out.c_str();
}
void asr_whisper_free(void* ctx) { whisper_free((whisper_context*)ctx); }
void* asr_parakeet_load(const char* path, int use_gpu, int dev) {
    parakeet_context_params p = parakeet_context_default_params();
    p.use_gpu = use_gpu; p.gpu_device = dev;
    return parakeet_init_from_file_with_params(path, p);
}
const char* asr_parakeet_run(void* ctx, const float* pcm, int n, int threads) {
    parakeet_full_params p = parakeet_full_default_params(PARAKEET_SAMPLING_GREEDY);
    p.n_threads = threads;
    out.clear();
    if (parakeet_full((parakeet_context*)ctx, p, pcm, n) != 0) return "";
    int ns = parakeet_full_n_segments((parakeet_context*)ctx);
    for (int i = 0; i < ns; i++) { if (i) out += " "; out += parakeet_full_get_segment_text((parakeet_context*)ctx, i); }
    return out.c_str();
}
void asr_parakeet_free(void* ctx) { parakeet_free((parakeet_context*)ctx); }
}
