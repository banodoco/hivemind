# Hivemind Retrieval — System Comparison

- Generated: `2026-07-29T00:00:00Z`
- Systems: `legacy`, `lexical`, `oracle`
- ks: [1, 5, 10]
- Recall/MRR/nDCG are macro-averaged over judged cases (failures count as 0). No-hit cases feed `no-hit satisfied`, not Recall.

## Overall relevance

| Metric | legacy | lexical | oracle | n |
|---|---|---|---|---|
| Recall@1 | 0.3413 | 0.5545 | 0.8317 | 104 |
| Recall@5 | 0.5769 | 0.7644 | 1.0000 | 104 |
| Recall@10 | 0.6058 | 0.7740 | 1.0000 | 104 |
| MRR | 0.4847 | 0.7155 | 1.0000 | 104 |
| nDCG@10 | 0.4954 | 0.7061 | 1.0000 | 104 |
| MAP | 0.4716 | 0.6873 | 1.0000 | 104 |

## Operational metrics

| Metric | legacy | lexical | oracle |
|---|---|---|---|
| zero-result rate | 0.4018 | 0.2500 | 0.0714 |
| failure rate | 0.0000 | 0.0000 | 0.0000 |
| timeout rate | 0.0000 | 0.0000 | 0.0000 |
| error rate | 0.0000 | 0.0000 | 0.0000 |
| no-hit satisfied | 0.7500 | 1.0000 | 1.0000 |
| Count | legacy | lexical | oracle |
|---|---|---|---|
| n_total | 112 | 112 | 112 |
| n_judged | 104 | 104 | 104 |
| n_no_hit | 8 | 8 | 8 |
| zero_result | 45 | 28 | 8 |
| timeouts | 0 | 0 | 0 |
| errors | 0 | 0 | 0 |
| failures | 0 | 0 | 0 |

## Latency (ms, successful calls only)

| Statistic | legacy | lexical | oracle |
|---|---|---|---|
| p50_ms | 0.049 | 1.005 | 0.001 |
| p95_ms | 0.072 | 1.129 | 0.003 |
| p99_ms | 0.122 | 1.205 | 0.004 |
| mean_ms | 0.051 | 0.851 | 0.002 |

## Per category — Recall@10 / nDCG@10 / MRR

| Category | legacy R@10 / nDCG / MRR | lexical R@10 / nDCG / MRR | oracle R@10 / nDCG / MRR |
|---|---|---|---|
| best_is_distillation | 0.0588 / 0.0588 / 0.0588 | 0.3235 / 0.3742 / 0.4118 | 1.0000 / 1.0000 / 1.0000 |
| best_is_message | 0.6471 / 0.5275 / 0.5216 | 0.7647 / 0.7068 / 0.6961 | 1.0000 / 1.0000 / 1.0000 |
| best_is_resource | 0.1250 / 0.0949 / 0.1250 | 0.6875 / 0.6868 / 0.7500 | 1.0000 / 1.0000 / 1.0000 |
| channel_scoped | 1.0000 / 0.9077 / 0.8750 | 1.0000 / 1.0000 / 1.0000 | 1.0000 / 1.0000 / 1.0000 |
| code_fragment | 1.0000 / 0.8930 / 0.8889 | 1.0000 / 0.8822 / 0.8750 | 1.0000 / 1.0000 / 1.0000 |
| cross_source | 0.1667 / 0.1667 / 0.1667 | 0.5000 / 0.6914 / 0.8333 | 1.0000 / 1.0000 / 1.0000 |
| exact_name | 0.9000 / 0.6281 / 0.5917 | 0.9200 / 0.7145 / 0.7000 | 1.0000 / 1.0000 / 1.0000 |
| long_resource_chunk | 1.0000 / 1.0000 / 1.0000 | 1.0000 / 1.0000 / 1.0000 | 1.0000 / 1.0000 / 1.0000 |
| multi_term | 0.0000 / 0.0000 / 0.0000 | 0.8000 / 0.7676 / 0.8000 | 1.0000 / 1.0000 / 1.0000 |
| named_author | 0.3750 / 0.2421 / 0.3750 | 0.7500 / 0.7085 / 0.7500 | 1.0000 / 1.0000 / 1.0000 |
| paraphrase | 0.0000 / 0.0000 / 0.0000 | 0.1000 / 0.1000 / 0.1000 | 1.0000 / 1.0000 / 1.0000 |
| pending_status | 0.0000 / 0.0000 / 0.0000 | 0.5000 / 0.5000 / 0.5000 | 1.0000 / 1.0000 / 1.0000 |
| selective_filter | 0.7727 / 0.6057 / 0.6061 | 0.9091 / 0.8195 / 0.8030 | 1.0000 / 1.0000 / 1.0000 |
| settings | 0.1429 / 0.1429 / 0.1429 | 0.5714 / 0.5714 / 0.5714 | 1.0000 / 1.0000 / 1.0000 |
| single_workflow | 1.0000 / 0.9385 / 0.9167 | 1.0000 / 1.0000 / 1.0000 | 1.0000 / 1.0000 / 1.0000 |
| snowflake | 0.6111 / 0.4981 / 0.4926 | 0.7500 / 0.7112 / 0.7130 | 1.0000 / 1.0000 / 1.0000 |
| spelling_variant | 1.0000 / 0.4981 / 0.3906 | 1.0000 / 0.7107 / 0.7500 | 1.0000 / 1.0000 / 1.0000 |
| time_scoped | 1.0000 / 0.6876 / 0.5556 | 1.0000 / 0.7269 / 0.6111 | 1.0000 / 1.0000 / 1.0000 |
| timeout_prone | 0.1667 / 0.1015 / 0.0667 | 0.0000 / 0.0000 / 0.0000 | 1.0000 / 1.0000 / 1.0000 |
| workflow_code | 0.8906 / 0.8273 / 0.8307 | 0.8906 / 0.8404 / 0.8464 | 1.0000 / 1.0000 / 1.0000 |
| workflow_only | 1.0000 / 0.8962 / 1.0000 | 1.0000 / 0.9002 / 0.9000 | 1.0000 / 1.0000 / 1.0000 |
| workflow_python_evidence | 0.6250 / 0.6250 / 0.6250 | 0.6250 / 0.6250 / 0.6250 | 1.0000 / 1.0000 / 1.0000 |

## Per-query diagnostics

| Case | Query | legacy top1 | lexical top1 | oracle top1 | legacy R@10 | lexical R@10 | oracle R@10 | outcome |
|---|---|---|---|---|---|---|---|---|
| G001 | FLUX.1 | 1764 | 1764 | 5 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G002 | Wan 2.2 | 169 | 169 | 169 | 0.5000 | 1.0000 | 1.0000 | O/O/O |
| G003 | Wan2.2 | 2542 | 169 | 8 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G004 | Flux 2 | 8 | 8 | 1757 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G005 | LTX-Video | 2540 | 25 | 4 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G006 | lightx2v_I2V_14B | 8 | 8 | 8 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G007 | Bernini | 10 | 10 | 83 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G008 | Qwen Image | 8 | 8 | 7 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G009 | VACE | 7 | 7 | 20 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G010 | CogVideoX | — | — | 64 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G011 | Mochi | 260 | 260 | 260 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G012 | Stable Diffusion | 240 | 240 | 36 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G013 | SDXL | 2757 | 11 | 11 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G014 | Hunyuan | 9 | 8 | 8 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G015 | Cascade | 240 | 240 | 240 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G016 | .gguf | 2539 | 2539 | 169 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G017 | Seedance 2 | 3 | 3 | 3 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G018 | WanVideoSampler | — | — | 20 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G019 | KSampler | 2758 | 2758 | 11 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G020 | VAEDecode | 1764 | 1764 | 64 | 0.5000 | 0.5000 | 1.0000 | O/O/O |
| G021 | BerniniConditioning | 83 | 83 | 83 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G022 | BlockifyMask | 2542 | 2542 | 169 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G023 | MochiDecodeSpatialTiling | 260 | 260 | 260 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G024 | DetailDaemonSamplerNode | 7 | 7 | 7 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G025 | AudioSeparation | 1167 | 1167 | 1167 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G026 | DepthAnythingV2Preprocessor | 2539 | 2539 | 2539 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G027 | IPAdapterFaceIDKolors | 2757 | 2757 | 2757 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G028 | ClownGuide_Beta | 240 | 240 | 240 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G029 | LoadWanVideoT5TextEncoder | 20 | 20 | 20 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G030 | EmptyHunyuanLatentVideo | 8 | 8 | 8 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G031 | Flux2Scheduler | 1757 | 1757 | 1757 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G032 | ltx-2-19b-ic-lora-detailer | 2540 | 2540 | 4 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G033 | wan2.2_animate_14B | 2542 | 2542 | 2542 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G034 | CreateVideo | 83 | 83 | 4 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G035 | which workflow wires the ColorMatch node | — | — | 2537 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G036 | find the ipadapter weight types workflow | — | — | 2758 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G037 | regional ipadapter mask conditioning | — | — | 2750 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G038 | controlnet inpainting flux | — | 5 | 5 | 0.0000 | 1.0000 | 1.0000 | O/O/O |
| G039 | video upscaling lora | — | 169 | 4 | 0.0000 | 1.0000 | 1.0000 | O/O/O |
| G040 | pose control animation | — | 169 | 169 | 0.0000 | 1.0000 | 1.0000 | O/O/O |
| G041 | audio separation multi talk | — | 1167 | 1167 | 0.0000 | 1.0000 | 1.0000 | O/O/O |
| G042 | face id kolors | — | 2757 | 2757 | 0.0000 | 1.0000 | 1.0000 | O/O/O |
| G043 | image comparison enhancement | — | 7 | 7 | 0.0000 | 1.0000 | 1.0000 | O/O/O |
| G044 | noise scheduling lora | — | 8 | 8 | 0.0000 | 1.0000 | 1.0000 | O/O/O |
| G045 | svd sdxl turbo | — | 11 | 11 | 0.0000 | 1.0000 | 1.0000 | O/O/O |
| G046 | make the motion less jittery | — | — | 5 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G047 | tighten control using a reference video | — | — | 3 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G048 | localize edits to just one region | — | — | 7 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G049 | keep colors consistent between the input and output character | — | — | 4 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G050 | is there a faster sampler patch | — | — | 6 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G051 | compare two image editors | — | — | 8 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G052 | what searchable info should a workflow keep | — | — | 12 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G053 | budget VRAM for training a control lora | — | 9 | 9 | 0.0000 | 1.0000 | 1.0000 | O/O/O |
| G054 | Bernini OOM fix | — | 10 | 10 | 0.0000 | 1.0000 | 1.0000 | O/O/O |
| G055 | ClownShark crash comfyui | — | — | 10 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G056 | INT8 quantization makes lora weak | — | — | 11 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G057 | looping sampler MSR | 1531487459404419102 | 1531487459404419102 | 1531487459404419102 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G058 | custom audio workflows | — | — | 1531711322037682237 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G059 | upscale | 1 | 1531607167076012122 | 1531607167076012122 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G060 | flux.2 dev lora | 1531723906308771967 | 1531723906308771967 | 1531724110558920724 | 0.5000 | 1.0000 | 1.0000 | O/O/O |
| G061 | vace t2v weights | — | 1531284476775370863 | 1531284476775370863 | 0.0000 | 1.0000 | 1.0000 | O/O/O |
| G062 | live update summary | — | — | 1531714016236409054 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G063 | controlnet | 1531344153131352206 | 1531344153131352206 | 1531344153131352206 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G064 | sampler | 1531693288141230103 | 1531693288141230103 | 1531693288141230103 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G065 | vae | 1531732489159184569 | 1531732489159184569 | 1531732489159184569 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G066 | upscale | 1 | 1531607167076012122 | 1531607167076012122 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G067 | lora | 11 | 11 | 1531732226474246244 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G068 | controlnet | 1531344153131352206 | 1531344153131352206 | 1531344153131352206 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G069 | mask | 7 | 7 | 1531728869349003414 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G070 | upscale ai generated video | — | 1 | 1 | 0.0000 | 0.5000 | 1.0000 | O/O/O |
| G071 | lightx2v motion trade off | — | 5 | 5 | 0.0000 | 0.5000 | 1.0000 | O/O/O |
| G072 | vace inpainting masks | — | 7 | 7 | 0.0000 | 0.5000 | 1.0000 | O/O/O |
| G073 | bernini conditioning video | — | 83 | 83 | 0.0000 | 0.5000 | 1.0000 | O/O/O |
| G074 | upscale step preserve detail | — | — | 1531607167076012122 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G075 | not seeing many flux.2 dev loras | 1531723906308771967 | 1531723906308771967 | 1531723906308771967 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G076 | audio vae only super and nano | — | 1531719890136993862 | 1531719890136993862 | 0.0000 | 1.0000 | 1.0000 | O/O/O |
| G077 | flux inpainting compositing | — | 5 | 5 | 0.0000 | 1.0000 | 1.0000 | O/O/O |
| G078 | mochi video input | — | 260 | 260 | 0.0000 | 1.0000 | 1.0000 | O/O/O |
| G079 | faceid kolors chatglm | — | — | 2757 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G080 | taeltx | 2540 | 2540 | 2540 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G081 | wan2.2_animate_14B_bf16 | 2542 | 2542 | 2542 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G082 | wan2.1_vace_14B | 2537 | 2537 | 2537 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G083 | Florence2 | 2538 | 2538 | 2538 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G084 | ColorMatch | 2537 | 2537 | 2537 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G085 | IPAdapter | 2758 | 2758 | 2750 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G086 | LTX | 2540 | 25 | 4 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G087 | Mochi | 260 | 260 | 260 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G088 | ColorMatch | 2537 | 2537 | 2537 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G089 | BerniniConditioning | 83 | 83 | 83 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G090 | BlockifyMask | 2542 | 169 | 169 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G091 | ipadapter_weight_types | 2758 | 2758 | 2758 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G092 | Flux2Scheduler | 1757 | 1757 | 1757 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G093 | taeltx | 2540 | 2540 | 2540 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G094 | ltxv | 2540 | 25 | 4 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G095 | wanvideo | 2542 | 30 | 20 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G096 | sd_xl | 2757 | 11 | 11 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G097 | control net | 2542 | 20 | 5 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G098 | flux1 | 1764 | 1764 | 5 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G099 | CFGGuider | 2538 | 2538 | 2538 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G100 | DownloadAndLoadFlorence2Model | 2538 | 2538 | 2538 | 1.0000 | 1.0000 | 1.0000 | O/O/O |
| G101 | model | 12 | 5 | 1531714016236409054 | 0.5000 | 0.0000 | 1.0000 | O/O/O |
| G102 | controlnet settings | — | — | 1531344153131352206 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G103 | best distillation for upscaling video | — | — | 1 | 0.0000 | 0.0000 | 1.0000 | O/O/O |
| G104 | attention tuner patch speed | — | 6 | 6 | 0.0000 | 1.0000 | 1.0000 | O/O/O |
| G105 | zzqxnotarealmodel-99999 | — | — | — | — | — | — | O/O/O |
| G106 | qwertyuiopasdfgh-nonexistent-term | — | — | — | — | — | — | O/O/O |
| G107 | DEFINITELY_NOT_A_REAL_NODE_CLASS_XYZ | — | — | — | — | — | — | O/O/O |
| G108 | BerniniConditioning | 83 | — | — | — | — | — | O/O/O |
| G109 | ColorMatch | 2537 | — | — | — | — | — | O/O/O |
| G110 | upscale | — | — | — | — | — | — | O/O/O |
| G111 | DROP TABLE unified_feed | — | — | — | — | — | — | O/O/O |
| G112 | 🛑🦄 imaginary-emoji-workflow-🦄🛑 | — | — | — | — | — | — | O/O/O |

<!-- Generated by eval.retrieval.compare. Ported structure (Pumpernickel) with Hivemind-owned metrics/adapters; see NOTICE.md. -->
