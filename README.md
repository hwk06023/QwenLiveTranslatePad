# QwenLiveTranslatePad

브라우저 마이크를 사용해 **Qwen3.8-LiveTranslate**로 실시간 양방향 음성 통역을 하는 프로토타입입니다.

- 직원: 한국어 → 선택한 민원인 언어
- 민원인: 자동 인식된 원어 → 한국어
- 실시간 원문/번역 자막
- Qwen이 생성한 번역 음성을 브라우저에서 스트리밍 재생
- API Key는 브라우저로 노출하지 않고 서버에만 저장
- 버튼을 누른 동안만 마이크 오디오 전송

## 구조

```text
Browser microphone
      │  PCM16 mono / 16 kHz
      ▼
FastAPI WebSocket proxy
      │
      ▼
Qwen3.8-LiveTranslate WebSocket
      │
      ├─ source transcription
      ├─ translated transcript
      └─ PCM16 mono / 24 kHz translated audio
                         │
                         ▼
                   Browser speaker
```

## 1. Clone

```bash
git clone https://github.com/hwk06023/QwenLiveTranslatePad.git
cd QwenLiveTranslatePad
```

## 2. 환경변수

```bash
cp .env.example .env
nano .env
```

필수 값:

```env
DASHSCOPE_API_KEY=YOUR_KEY
DASHSCOPE_WORKSPACE_ID=YOUR_WORKSPACE_ID
DASHSCOPE_REGION=ap-southeast-1
QWEN_MODEL=qwen3.8-livetranslate-flash-realtime
```

`.env`는 `.gitignore`에 포함되어 있으므로 GitHub에 올리지 마세요.

## 3. Ubuntu / Jetson 설치

```bash
sudo bash scripts/install.sh
```

테스트 실행:

```bash
bash scripts/run.sh
```

서버 내부에서 확인:

```bash
curl http://127.0.0.1:8080/health
```

## 4. 서비스로 실행

```bash
sudo systemctl enable --now qwen-live-translate
sudo systemctl status qwen-live-translate
journalctl -u qwen-live-translate -f
```

기본적으로 `127.0.0.1:8080`에서 대기합니다.

## 5. HTTPS 연결

브라우저의 `getUserMedia()` 마이크 권한은 일반적으로 HTTPS가 필요합니다. `thor.alango.xyz`가 이미 웹 터미널을 서비스하고 있다면 그 설정을 덮어쓰지 말고, 예를 들어 아래처럼 별도 호스트를 권장합니다.

```text
translate.alango.xyz -> nginx/Cloudflare -> 127.0.0.1:8080
```

`deploy/nginx.conf`에 reverse proxy 예제가 있습니다. 실제 TLS는 Cloudflare 또는 Certbot 등 현재 인프라에 맞춰 적용하세요.

## 사용법

1. 민원인 언어를 고릅니다.
2. 직원이 **누른 채 한국어로 말하기** 버튼을 누르고 말합니다.
3. 버튼을 떼면 마지막 음성을 처리하고 번역 음성이 재생됩니다.
4. 민원인이 **누른 채 말하기** 버튼을 사용하면 한국어로 번역됩니다.

현재 구현은 한 번의 push-to-talk마다 하나의 Qwen realtime session을 엽니다. 먼저 기능 검증하기 좋은 구조이며, 이후 필요하면 장시간 persistent session, 듀얼 디스플레이, 행정/법률 용어 보강, 상담 로그 저장 정책 등을 추가할 수 있습니다.

## API 동작

Qwen3.8-LiveTranslate 입력은 PCM16 16 kHz mono로 전송하며, 번역 텍스트와 PCM16 24 kHz 번역 음성을 받아 브라우저에 전달합니다. backend가 Alibaba Cloud WebSocket과 브라우저 WebSocket 사이를 프록시하므로 API Key는 클라이언트에 노출되지 않습니다.

## 보안/개인정보

- `DASHSCOPE_API_KEY`는 `.env`에만 저장하고 Git에 커밋하지 않습니다.
- 현재 앱은 상담 오디오나 텍스트를 파일/DB에 저장하지 않습니다.
- 공공 민원 환경에 배포할 경우 인증, 접근제어, 보존정책, 로그 마스킹을 별도로 검토하세요.

## 문제 해결

### `403` / 인증 오류

API Key와 Workspace ID가 동일한 Alibaba Cloud 리전/Workspace에 속하는지 확인하세요.

### 마이크 권한이 안 뜸

`http://서버IP:8080` 같은 비보안 origin에서는 브라우저가 마이크를 차단할 수 있습니다. HTTPS 도메인으로 접속하세요.

### 번역 음성이 안 들림

브라우저 탭 음소거, OS 출력장치, autoplay 정책을 확인하세요. 이 앱은 사용자의 버튼 입력으로 AudioContext를 활성화합니다.
