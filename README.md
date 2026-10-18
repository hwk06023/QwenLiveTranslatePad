# QwenLiveTranslatePad

브라우저 마이크로 민원 창구의 직원과 민원인 사이를 **실시간 양방향 음성 통역**하는 패드입니다. 번역 엔진은 두 가지이며 웹에서 전환합니다.

| 엔진 | 동작 | 특징 |
| --- | --- | --- |
| **On-prem GPU** (기본) | Jetson Thor GPU에서 Qwen3-ASR → Qwen3-4B-Instruct → Qwen3-TTS | 음성이 외부로 나가지 않음, 민원인 언어 자동 감지, 10개 언어 음성 |
| **Cloud API** | Alibaba Cloud `qwen3.8-livetranslate-flash-realtime` WebSocket | 말하는 도중에 스트리밍, 30개 언어 |

- 직원: 한국어 → 선택한 민원인 언어
- 민원인: 원어 → 한국어 (on-prem은 감지한 언어로 직원 답변 언어도 자동 전환)
- 실시간 원문/번역 자막과 번역 음성 재생
- `/` 시연 페이지: 시연 위주 화면, 엔진 토글 (관리자가 끌 수 있음)
- `/admin` 관리자 콘솔: 턴별 지연시간 타임라인, 엔진별 p50/p90, 단계별 시간(ASR/LLM/TTS), GPU 사용률·전력·메모리, 기본 엔진 전환, 모델 로드/언로드

## 구조

```text
Browser mic (PCM16 16 kHz)                       Browser speaker (PCM16 24 kHz)
      │                                                     ▲
      ▼                                                     │
FastAPI  /api/ws/translate?engine=local|api  ── ClientChannel (턴별 지연 기록) ──┐
      │                                                                         │
      ├─ local ─ Qwen3-ASR-1.7B ─ Qwen3-4B-Instruct-2507 (토큰 스트리밍) ─ Qwen3-TTS-1.7B (오디오 스트리밍)
      │          └ 녹음 중 1.5초마다 부분 자막          └ 문장이 완성되는 대로 합성 시작, 0.5초 분량마다 전송
      │
      └─ api ─── Qwen3.8-LiveTranslate realtime WebSocket (Alibaba Cloud)
```

| 경로 | 내용 |
| --- | --- |
| `app/main.py` | 라우트, 엔진 선택, 관리자 API, `public/` 정적 서빙 |
| `app/local_engine.py` | On-prem 파이프라인 (모델 로딩, 부분 자막, LLM→TTS 파이프라이닝) |
| `app/tts_speedup.py` | Qwen3-TTS 가속: 서브토커 CUDA Graph, 코덱 프레임 스트리밍 디코딩 |
| `app/qwen_realtime.py` | Cloud API 브리지 |
| `app/metrics.py` | 턴별 타임스탬프/단계 시간 기록, 집계 (대화 내용은 저장하지 않음) |
| `app/system_stats.py` | nvidia-smi, `/proc/meminfo` 기반 GPU/메모리 지표 |
| `app/admin.py` | 관리자 쿠키 인증, 런타임 설정 (`runtime_settings.json`) |
| `public/` | 프런트엔드 (`index.html` 시연, `admin.html` 관리자, 공용 `translate-client.js`) |

## On-prem 성능 (Jetson AGX Thor, 실측)

| 항목 | 값 |
| --- | --- |
| 모델 로드 (콜드 스타트) | 약 26초 (ASR 4s · LLM 6s · TTS 5s · 워밍업 3s), GPU 텐서 메모리 16.4 GB |
| 최종 ASR (5초 발화) | 0.6–0.7초 |
| 번역 LLM | 첫 토큰 50–200ms, 약 23 tok/s |
| TTS | 첫 오디오 청크 0.4–0.6초, RTF 0.7 (실시간보다 빠름) |
| **버튼 해제 → 첫 번역 음성** | **p50 약 1.7초** (브라우저 체감 1.64초), 긴 번역 약 2.2초 |

TTS 최적화 내용(`app/tts_speedup.py`):

- Qwen3-TTS는 80ms 프레임마다 서브토커가 15개 코드북을 HF `generate()`로 생성하는데, Thor에서는 GPU가 아니라 파이썬 오버헤드가 병목(RTF 1.12)이었습니다. 이 15스텝 루프를 StaticCache + **CUDA Graph**로 캡처해 RTF 0.66으로 줄였습니다.
- 디코더가 인과적이므로 생성 중인 코덱 프레임을 72프레임 왼쪽 문맥으로 **증분 디코딩**해 스트리밍합니다(오프라인 디코딩과 38dB 이상 일치). 첫 음성이 문장 길이와 무관하게 빨라집니다.
- 짧은 문장에서 샘플링이 폭주하는 현상(예: "こんにちは。"가 29초 오디오가 됨)을 막기 위해 10자 미만 조각은 다음 문장과 합치고, 텍스트 길이 기반으로 생성 길이 상한을 둡니다.

## 설치 (Ubuntu / Jetson Thor)

```bash
git clone https://github.com/hwk06023/QwenLiveTranslatePad.git
cd QwenLiveTranslatePad
cp .env.example .env && nano .env      # ADMIN_PASSWORD 필수, Cloud API 쓰려면 DASHSCOPE_* 입력
sudo bash scripts/install.sh           # venv, 기본 의존성, systemd 서비스
bash scripts/install-local.sh          # PyTorch(CUDA 13) + Qwen ASR/TTS + 모델 다운로드 (~16 GB)
bash scripts/run.sh                    # http://127.0.0.1:8090
```

- Cloud API만 쓸 경우 `install-local.sh`는 생략하고 `LOCAL_ENGINE_ENABLED=false`로 둡니다.
- `LOCAL_PRELOAD=true`면 서버 시작 시 모델을 GPU에 올립니다(약 1분). 아니면 첫 턴에 로드합니다.
- 모델은 `models/<이름>`에 저장되며 git에서 제외됩니다. 다른 경로/모델은 `LOCAL_*_MODEL`로 지정합니다.
- 호스트 전역 `HF_HOME`이 쓰기 불가한 디스크를 가리키면 자동으로 `~/.cache/huggingface`를 사용합니다.
- 브라우저 없이 확인: `.venv/bin/python scripts/smoke_test.py speech.wav --target en --engine local` (16kHz mono WAV)

### 서비스 실행

```bash
sudo systemctl enable --now qwen-live-translate
journalctl -u qwen-live-translate -f
```

기본 포트는 `127.0.0.1:8090`입니다(Thor의 8080은 기존 로컬 통역 컨테이너가 사용 중).

### HTTPS

브라우저 마이크(`getUserMedia`)는 HTTPS가 필요합니다. `deploy/nginx.conf`의 reverse proxy 예시처럼 `translate.alango.xyz → 127.0.0.1:8090`로 연결하고 TLS는 Cloudflare/Certbot 등으로 적용하세요. WebSocket 경로는 `/api/ws/` (구 경로 `/ws/`도 지원)입니다.

## 관리자 콘솔 (`/admin`)

`.env`의 `ADMIN_PASSWORD`로 로그인합니다(비어 있으면 관리자 기능 비활성). 세션은 HMAC 서명된 HttpOnly 쿠키로 12시간 유지됩니다.

- **엔진 설정**: 시연 페이지 기본 엔진, 시연 페이지 엔진 토글 허용 여부
- **온프렘 엔진**: 모델/디바이스, 로딩 시간(ASR/LLM/TTS/warmup), GPU 텐서 메모리, 로드·언로드
- **시스템**: GPU 사용률·전력 스파크라인, 통합 메모리, 클럭·온도, CPU 부하
- **라이브 테스트 콘솔**: 엔진을 골라 직접 말해보고 해당 턴의 타임라인과 브라우저 체감 지연(버튼 해제 → 스피커 재생) 확인
- **지연시간 요약**: 엔진별 p50/p90 — 연결, 해제→자막, 해제→첫 번역, 해제→첫 음성, 해제→완료 + 단계별 p50
- **최근 턴**: 턴별 상세(ASR, LLM TTFT, tok/s, TTS 첫 문장, RTF, GPU 대기 등)

지표는 메모리에만 최근 300턴을 보관하며 원문/번역 텍스트는 저장하지 않습니다(문자 수만 기록).

## API

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET | `/api/health` | 엔진별 가용 여부 |
| GET | `/api/config` | 시연 페이지 설정(엔진, 지원 언어) |
| WS | `/api/ws/translate?target=en&engine=local&direction=staff&source=ko` | 한 번의 push-to-talk 턴 |
| POST | `/api/admin/login` · `/logout` | 관리자 세션 |
| GET | `/api/admin/overview` · `/system` | 대시보드 데이터 |
| POST | `/api/admin/settings` · `/local/load` · `/local/unload` | 설정 변경, 모델 관리 |

WebSocket 메시지: 클라이언트는 PCM16 바이너리와 `{"type":"finish"}`를 보내고, 서버는 `engine`, `ready`, `processing`, `source_partial`, `source_delta`, `source_done`, `translation_delta`, `translation_done`, `audio`, `response_done`, `session_finished`, `error` (관리자에게는 `metrics`)를 보냅니다.

## Vercel

`scripts/deploy-vercel.sh`로 배포하면 `public/`은 CDN, `/api/*`는 `api/index.py`가 처리합니다. 서버리스에는 GPU가 없으므로 Cloud API 엔진만 동작합니다. 관리자 기능을 쓰려면 Vercel 환경변수에 `ADMIN_PASSWORD`를 추가하세요.

## 보안/개인정보

- API Key와 관리자 비밀번호는 `.env`에만 두고 커밋하지 않습니다.
- 상담 오디오/텍스트는 파일·DB에 저장하지 않습니다. On-prem 엔진은 음성을 외부로 보내지 않습니다.
- 공공 민원 환경에 배포할 경우 접근제어, 보존정책, 로그 마스킹을 별도로 검토하세요.

## 문제 해결

- **On-prem 엔진이 비활성**: `/admin`의 온프렘 카드 오류 메시지를 확인하세요. `bash scripts/install-local.sh` 재실행, `nvidia-smi` 확인.
- **첫 번역이 느림**: 모델이 로드되지 않은 상태입니다. `LOCAL_PRELOAD=true` 또는 관리자 콘솔에서 *모델 로드*.
- **Cloud API `403`**: API Key와 Workspace ID가 같은 리전/Workspace인지 확인하세요.
- **마이크 권한이 안 뜸**: HTTPS로 접속하세요(`localhost`는 예외).
- **번역 음성이 안 들림**: 탭 음소거, OS 출력장치, autoplay 정책 확인.
